"""One real Wander observation, using AI's existing sensory tools and evidence.

Selection has the full activity context. Images are analyzed through the shared
vision boundary and never stored in runtime payloads; sharing belongs to the
ordinary review/settlement/delivery lifecycle, not this executor.
"""
import inspect
import json

from behavior_scheduler.sensory_execution import CHECKUP_TOOLS, execute_tool_with_receipt
from behavior_scheduler.tool_standardizer import ToolCallStandardizer
from checkup_orchestrator import (
    CHECKUP_TOOL_GUIDANCE, sensory_tool_items, scheduled_conclusion, grounded_tracking_fact,
)
from visual_dialogue import (
    generate_visual_dialogue, tool_visual_images, apply_visual_receipts, fail_visual_receipts,
)
from .runtime_decision_adapters import _DecisionAdapterBase, _runtime_block
from .flash_structured import FlashJsonResult
from .runtime_store import redact_sensitive
from .checkup_selection_context import passive_selection_context
from checkup_assessment import source_status


def coalesce_sensory_calls(calls, standardizer):
    """Collapse duplicate reads and combine Band's snapshot metrics in order."""
    from behavior_scheduler.band_tool import BandTool

    unique, seen = [], set()
    for name, parameters in calls:
        parameters = dict(parameters)
        if name == 'band':
            action = parameters.get('action')
            parameters['action'] = BandTool._ACTION_MAP.get(action, action)
            if parameters['action'] != 'heart_rate_continuous':
                parameters.pop('duration', None)  # Only continuous heart rate uses it.
        key = (name, json.dumps(parameters, sort_keys=True, ensure_ascii=False))
        if key not in seen:
            seen.add(key)
            unique.append((name, parameters))

    snapshots = {'heart_rate', 'steps', 'sleep', 'spo2', 'all_health'}
    positions = [i for i, (name, params) in enumerate(unique)
                 if name == 'band' and params.get('action') in snapshots]
    if len(positions) > 1:
        combined = standardizer.standardize({'tool': 'band', 'parameters': {'action': 'all_health'}})
        if combined.is_valid:
            unique[positions[0]] = ('band', combined.parameters)
            unique = [call for i, call in enumerate(unique) if i not in positions[1:]]
    return unique


async def registered_sensory_tools():
    from behavior_scheduler import get_global_scheduler
    scheduler = await get_global_scheduler()
    return {name: tool for name, tool in scheduler.tools.items() if name in CHECKUP_TOOLS}


class WanderCheckupExecutor(_DecisionAdapterBase):
    def __init__(self, store, *, tools_provider=registered_sensory_tools,
                 passive_context_provider=passive_selection_context, **kwargs):
        super().__init__(store, **kwargs)
        self.tools_provider = tools_provider
        self.passive_context_provider = passive_context_provider

    async def observe(self, activity, node, context):
        from .node_execution_adapter import NodeExecutionResult, NodeExecutionStatus
        run = self.store.get_run(activity['run_id'])
        try:
            tools = self.tools_provider()
            if inspect.isawaitable(tools):
                tools = await tools
        except RuntimeError:
            tools = {}
        tools = {name: tool for name, tool in tools.items()
                 if name in CHECKUP_TOOLS and getattr(tool, 'visible_to_pro', True)}
        runtime = _runtime_block(context, activity, self.store.list_nodes(activity['activity_id']),
                                 phase='sensory_selection', run=run, current_node=node)
        runtime += '\n' + self.passive_context_provider(now=self.clock())
        runtime += '\n[本次可用感官]\n' + json.dumps([
            {'tool': name, 'description': tool.description, 'parameters': tool.parameters_schema}
            for name, tool in tools.items()
        ], ensure_ascii=False)
        instruction = (
            '根据本次活动起意和新鲜状态，决定这次需要取得哪些实时信息。' + CHECKUP_TOOL_GUIDANCE
            + '只输出 JSON：{"reason":"本次信息缺口", "actions":[{"tool":"可用感官名", "parameters":{}}]}。'
              'actions 是本次要执行的一组感官；同工具同参数的重复请求会合并为一次。'
              '同时查询心率、步数、睡眠或血氧时可用 band 的 all_health 一次取得；'
              '连续心率和每日趋势保留各自的查询参数。无需采集时可为空并说明原因。'
              '是否截图取决于是否需要屏幕内容，是否使用摄像头取决于是否需要本人或周围环境的画面。'
              '这里选择尚未执行的动作；真实材料将在节点复核中交回给你。'
        )
        messages = await self._messages(context, run, runtime, instruction, activity)
        from . import node_retry
        correcting = False
        while True:
            decision = (await self.llm_caller(messages=messages, temperature=0.0, max_tokens=16384,
                                             route_label='wander_checkup_selection')) if tools else FlashJsonResult(status='unavailable')
            raw = decision.parsed if decision.status == 'ok' and isinstance(decision.parsed, dict) else {}
            actions = raw.get('actions') if tools else []
            validated, errors = [], []
            if not isinstance(actions, list):
                errors.append('invalid_sensory_decision')
            else:
                standardizer = ToolCallStandardizer(tools)
                for action in actions:
                    if not isinstance(action, dict) or not isinstance(action.get('tool'), str) or action['tool'] not in tools:
                        errors.append('unavailable_sensory_tool')
                        continue
                    call = standardizer.standardize(action)
                    if not call.is_valid:
                        errors.append('invalid_sensory_parameters')
                        continue
                    validated.append((call.tool_name, call.parameters))
                if not errors:
                    validated = coalesce_sensory_calls(validated, standardizer)
            if not errors or decision.status != 'ok':
                break
            note = {'invalid_sensory_decision': '没读出要使用的感官列表。',
                    'unavailable_sensory_tool': '选的感官不在这次可用的列表里。',
                    'invalid_sensory_parameters': '有一项感官参数对不上说明。'}[errors[0]]
            if not node_retry.take('选择感官', note):
                if correcting and not node_retry.cancelled():
                    node_retry.unresolved(note)
                break
            messages = [*messages, {'role': 'user', 'content': '【感官选择回执】' + note + ' 可以重选，也可以留空。'}]
            correcting = True
        receipts = []
        def interrupted():
            return bool((self.store.get_activity(activity['activity_id']) or {}).get('interrupt_requested'))

        if not errors:
            for name, parameters in validated:
                # A reply during selection/collection ends this node without
                # starting any not-yet-executed source. Recovery never replays it.
                if interrupted():
                    break
                _, receipt = await execute_tool_with_receipt(tools, name, parameters)
                receipts.append(receipt)
        images = [image for receipt in receipts for image in tool_visual_images(receipt)]
        if images and not interrupted():
            try:
                visual = await generate_visual_dialogue(messages, images,
                    turn_text='本次漫想感知的实际画面；按来源说明看到了什么及不确定之处。', allow_tools=False)
                apply_visual_receipts(receipts, visual['vision_envelope'])
            except Exception:
                fail_visual_receipts(receipts)
        elif images:
            # The capture really happened; a reply stops starting further
            # model calls. Retain captured-but-unanalyzed source evidence.
            apply_visual_receipts(receipts, {'sources': {}})
        items = redact_sensitive(sensory_tool_items(receipts))
        payload = {'checkup_version': 1, 'checkup_items': items,
                   'conclusion': scheduled_conclusion(items), 'intent': str(raw.get('reason') or ''),
                   'source_status': source_status(items), 'goal_assessment': {'status': 'not_assessed'},
                   'origin': 'wander', 'selection_errors': errors,
                   'observed_at': self.clock().isoformat()}
        summary = grounded_tracking_fact(payload)
        # The node is persisted by the execution adapter. Its complete source
        # receipts and later feelings share the activity's durable event card;
        # observation does not create a second conversation event here.
        acquired = any(item.get('status') == 'success' for item in items)
        was_interrupted = interrupted()
        payload['interrupt_requested'] = was_interrupted
        # A completed attempt with unknown evidence is a natural stop, not a
        # broken execution chain that should enter error backoff.
        status = (NodeExecutionStatus.SKIPPED if was_interrupted else
                  NodeExecutionStatus.FAILED if errors else
                  NodeExecutionStatus.SUCCEEDED if acquired else NodeExecutionStatus.SKIPPED)
        return NodeExecutionResult(
            status,
            completion_signal='observation_recorded' if acquired else '',
            summary=summary, payload=payload, retry_safe=False,
            side_effect_refs={'runtime_activity_id': activity['activity_id'],
                              'sensory_tools': [receipt['tool'] for receipt in receipts]},
            error=('interrupt_requested' if was_interrupted else errors[0] if errors else
                   '' if acquired else 'no_confirmed_sensory_evidence'),
            audit_model=decision.model, audit_input={'runtime': runtime},
            audit_raw_output=raw, audit_reasoning=decision.reasoning, audit_usage=decision.usage,
        )
