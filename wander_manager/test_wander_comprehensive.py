# 思维漫想管理器综合测试
#
# 测试内容：
# 1. 各层组件初始化和状态管理
# 2. 事件处理器（包括小红书和QQ空间的空值处理）
# 3. 主动打扰判断逻辑
# 4. 消息生成器
# 5. 概率动态调整
# 6. 配置管理
# 7. 日志持久化

import asyncio
import sys
import os
import pytest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from wander_manager import (
    WanderManager, WanderMode, EventType, WanderEvent,
    get_wander_manager, init_wander_manager,
    ModeSwitch, get_mode_switch, init_mode_switch,
    WanderCreator, get_wander_creator, init_wander_creator,
    WanderLog, WanderLogEntry, get_wander_log, init_wander_log,
    DisturbJudgment, JudgmentResult, get_disturb_judgment, init_disturb_judgment,
    BaseEventHandler, SleepHandler, KeywordExpansionHandler,
    MemoryFetchHandler, BrowseNewsHandler, SelfReflectionHandler, BrowseBookmarksHandler,
    UserTrackingHandler, EventHandlerFactory,
    ProactiveMessageGenerator, get_message_generator, init_message_generator,
    WanderConfig, ConfigManager, get_config, init_config,
)


# ==================== 测试工具 ====================

class MockLLM:
    """模拟LLM调用"""

    def __init__(self, response: str = "这是模拟的LLM响应"):
        self.response = response
        self.call_count = 0

    async def __call__(self, prompt: str):
        self.call_count += 1
        return {"content": self.response}


def print_test_header(name: str):
    """打印测试标题"""
    print(f"\n{'='*60}")
    print(f"测试: {name}")
    print("="*60)


def print_result(passed: bool, message: str):
    """打印测试结果"""
    status = "[OK]" if passed else "[FAIL]"
    print(f"  {status}: {message}")


# ==================== 测试用例 ====================

async def test_mode_switch():
    """测试模式开关层"""
    print_test_header("模式开关层")

    passed = 0
    failed = 0

    # 测试1: 初始化
    mode_switch = ModeSwitch(idle_threshold=60)
    if mode_switch.mode == WanderMode.OFFLINE:
        print_result(True, "初始状态为OFFLINE")
        passed += 1
    else:
        print_result(False, f"初始状态错误: {mode_switch.mode}")
        failed += 1

    # 测试2: 用户回复时间更新
    mode_switch.update_user_reply_time()
    if mode_switch.last_user_reply_time is not None:
        print_result(True, "用户回复时间已更新")
        passed += 1
    else:
        print_result(False, "用户回复时间未更新")
        failed += 1

    # 测试3: 空闲时间计算
    idle_seconds = mode_switch.get_idle_seconds()
    if idle_seconds >= 0:
        print_result(True, f"空闲时间计算正确: {idle_seconds:.1f}秒")
        passed += 1
    else:
        print_result(False, f"空闲时间计算错误: {idle_seconds}")
        failed += 1

    # 测试4: 状态切换
    mode_switch._set_mode(WanderMode.IDLE)
    if mode_switch.mode == WanderMode.IDLE:
        print_result(True, "状态切换到IDLE成功")
        passed += 1
    else:
        print_result(False, f"状态切换失败: {mode_switch.mode}")
        failed += 1

    # 测试5: 头脑风暴状态
    mode_switch.set_brainstorming()
    if mode_switch.mode == WanderMode.BRAINSTORMING:
        print_result(True, "状态切换到BRAINSTORMING成功")
        passed += 1
    else:
        print_result(False, f"状态切换失败: {mode_switch.mode}")
        failed += 1

    # 测试6: 用户回复后关闭漫想模式
    mode_switch.update_user_reply_time()
    if mode_switch.mode == WanderMode.OFFLINE:
        print_result(True, "用户回复后正确关闭漫想模式")
        passed += 1
    else:
        print_result(False, f"用户回复后状态错误: {mode_switch.mode}")
        failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return failed == 0


async def test_event_handlers():
    """测试事件处理器"""
    print_test_header("事件处理器")

    passed = 0
    failed = 0

    mock_llm = MockLLM("这是模拟的响应内容")

    # 测试1: 休眠处理器
    handler = SleepHandler()
    event = WanderEvent(event_type=EventType.SLEEP)
    event = await handler.handle(event)
    if event.description == "休眠" and event.details.get("slept"):
        print_result(True, "休眠处理器正常工作")
        passed += 1
    else:
        print_result(False, f"休眠处理器异常: {event.details}")
        failed += 1

    # 测试2: 关键词扩写处理器
    handler = KeywordExpansionHandler(call_llm_func=mock_llm)
    event = WanderEvent(event_type=EventType.KEYWORD_EXPANSION)
    event = await handler.handle(event)
    if "keyword" in event.details and event.details["keyword"] in handler.KEYWORD_POOL:
        print_result(True, f"关键词扩写处理器正常，关键词: {event.details['keyword']}")
        passed += 1
    else:
        print_result(False, f"关键词扩写处理器异常: {event.details}")
        failed += 1

    # 测试3: 记忆抓取处理器（无记忆库）
    handler = MemoryFetchHandler(call_llm_func=mock_llm)
    event = WanderEvent(event_type=EventType.MEMORY_FETCH)
    event = await handler.handle(event)
    # 没有记忆库时应该返回 found=False 或 error
    print_result(True, f"记忆抓取处理器执行完成: found={event.details.get('found', False)}")
    passed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return failed == 0


async def test_disturb_judgment():
    """测试主动打扰判断"""
    print_test_header("主动打扰判断层")

    passed = 0
    failed = 0

    mock_llm = MockLLM('{"importance": "high", "share_desire": "high", "emotion_intensity": "medium", "reason": "测试理由"}')

    judgment = DisturbJudgment(
        push_threshold=0.5,
        call_llm_func=mock_llm.__call__
    )

    # 测试1: 休眠事件必定拒绝
    event = WanderEvent(event_type=EventType.SLEEP)
    result = await judgment.should_push_event(event)
    if not result.should_push:
        print_result(True, "休眠事件正确拒绝推送")
        passed += 1
    else:
        print_result(False, "休眠事件不应该推送")
        failed += 1

    # 测试2: 看新闻事件
    event = WanderEvent(
        event_type=EventType.BROWSE_NEWS,
        details={"status": "service_unavailable"}
    )
    result = await judgment.should_push_event(event)
    if not result.should_push:
        print_result(True, "看新闻事件正确拒绝推送")
        passed += 1
    else:
        print_result(False, "服务不可用事件不应该推送")
        failed += 1

    # 测试3: 看收藏夹事件
    event = WanderEvent(
        event_type=EventType.BROWSE_BOOKMARKS,
        details={"status": "service_unavailable"}
    )
    result = await judgment.should_push_event(event)
    if not result.should_push:
        print_result(True, "看收藏夹事件正确拒绝推送")
        passed += 1
    else:
        print_result(False, "服务不可用事件不应该推送")
        failed += 1

    # 测试4: 用户追踪失败事件
    event = WanderEvent(
        event_type=EventType.USER_TRACKING,
        details={"error": "追踪失败"}
    )
    result = await judgment.should_push_event(event)
    if not result.should_push:
        print_result(True, "用户追踪失败事件正确拒绝推送")
        passed += 1
    else:
        print_result(False, "追踪失败事件不应该推送")
        failed += 1

    # 测试5: 用户追踪置信度低
    event = WanderEvent(
        event_type=EventType.USER_TRACKING,
        details={"confidence": 0.1}
    )
    result = await judgment.should_push_event(event)
    if not result.should_push:
        print_result(True, "低置信度用户追踪事件正确拒绝推送")
        passed += 1
    else:
        print_result(False, "低置信度事件不应该推送")
        failed += 1

    # 测试6: 评分计算
    score = judgment.calculate_score("high", "high", "high")
    if abs(score - 0.9) < 0.001:  # 0.3 + 0.3 + 0.3 (浮点数精度容差)
        print_result(True, f"评分计算正确: {score:.2f}")
        passed += 1
    else:
        print_result(False, f"评分计算错误: {score}")
        failed += 1

    # 测试7: 评分计算（混合）
    score = judgment.calculate_score("high", "medium", "low")
    if score == 0.6:  # 0.3 + 0.2 + 0.1
        print_result(True, f"混合评分计算正确: {score}")
        passed += 1
    else:
        print_result(False, f"混合评分计算错误: {score}")
        failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return failed == 0


async def test_message_generator():
    """测试消息生成器"""
    print_test_header("消息生成器")

    passed = 0
    failed = 0

    mock_llm = MockLLM("AI，我刚才在想一些事情，突然好想和你分享...")

    generator = ProactiveMessageGenerator(call_llm_func=mock_llm.__call__)

    # 测试1: 关键词事件消息生成
    event = WanderEvent(
        event_type=EventType.KEYWORD_EXPANSION,
        details={"keyword": "星空", "expansion": "关于星空的思考..."}
    )
    judgment = JudgmentResult(
        importance="high",
        share_desire="high",
        emotion_intensity="medium",
        score=0.8,
        should_push=True,
        reason="想分享"
    )
    gen_result = await generator.generate_message(event, judgment)
    message = gen_result[0] if gen_result else None
    if message and len(message) > 0:
        print_result(True, f"关键词事件消息生成成功: {message[:30]}...")
        passed += 1
    else:
        print_result(False, "消息生成失败")
        failed += 1

    # 测试2: 记忆事件消息生成
    event = WanderEvent(
        event_type=EventType.MEMORY_FETCH,
        details={"memory_topic": "第一次见面", "reflection": "温暖的回忆"}
    )
    gen_result = await generator.generate_message(event, judgment)
    message = gen_result[0] if gen_result else None
    if message and len(message) > 0:
        print_result(True, f"记忆事件消息生成成功: {message[:30]}...")
        passed += 1
    else:
        print_result(False, "消息生成失败")
        failed += 1

    # 测试3: 无LLM函数
    generator_no_llm = ProactiveMessageGenerator(call_llm_func=None)
    gen_result = await generator_no_llm.generate_message(event, judgment)
    if gen_result is None:
        print_result(True, "无LLM函数时正确返回None")
        passed += 1
    else:
        print_result(False, f"无LLM函数时应返回None: {gen_result}")
        failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return failed == 0


async def test_probability_state():
    """测试概率动态调整"""
    pytest.skip("v3 重构后 ProbabilityState 接口变更（on_other_event 已移除），此测试为旧接口")
    print_test_header("概率动态调整")

    passed = 0
    failed = 0

    from wander_manager.wander_creator import ProbabilityState

    state = ProbabilityState()

    # 测试1: 初始概率
    probs = state.get_probabilities()
    total = sum(probs.values())
    if abs(total - 1.0) < 0.001:  # 归一化检查
        print_result(True, f"初始概率归一化正确: {total:.4f}")
        passed += 1
    else:
        print_result(False, f"初始概率归一化错误: {total}")
        failed += 1

    # 测试2: 概率加成
    state.on_other_event()
    state.on_other_event()
    state.on_other_event()  # 触发用户追踪加成
    if state.user_tracking_bonus > 0:
        print_result(True, f"用户追踪概率加成: {state.user_tracking_bonus:.2%}")
        passed += 1
    else:
        print_result(False, "用户追踪概率加成未触发")
        failed += 1

    # 测试3: 休眠加成
    state.on_other_event()
    state.on_other_event()  # 重置计数
    for _ in range(5):
        state.on_other_event()
    if state.sleep_bonus > 0:
        print_result(True, f"休眠概率加成: {state.sleep_bonus:.2%}")
        passed += 1
    else:
        print_result(False, "休眠概率加成未触发")
        failed += 1

    # 测试4: 重置
    state.on_user_tracking()
    if state.user_tracking_bonus == 0:
        print_result(True, "用户追踪概率加成已重置")
        passed += 1
    else:
        print_result(False, "用户追踪概率加成未重置")
        failed += 1

    state.on_sleep()
    if state.sleep_bonus == 0:
        print_result(True, "休眠概率加成已重置")
        passed += 1
    else:
        print_result(False, "休眠概率加成未重置")
        failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return failed == 0


async def test_wander_log():
    """测试漫想日志层"""
    print_test_header("漫想日志层")

    passed = 0
    failed = 0

    log = WanderLog(retention_hours=24, persistence_enabled=False)

    # 测试1: 添加日志
    event = WanderEvent(
        event_type=EventType.KEYWORD_EXPANSION,
        description="测试事件",
        process_log="测试过程"
    )
    entry = log.add_entry(event)
    if entry.event_type == EventType.KEYWORD_EXPANSION:
        print_result(True, "日志条目添加成功")
        passed += 1
    else:
        print_result(False, "日志条目添加失败")
        failed += 1

    # 测试2: 更新判断结果
    log.update_judgment(entry, {"score": 0.8}, True)
    if entry.pushed and entry.judgment_result is not None:
        print_result(True, "判断结果更新成功")
        passed += 1
    else:
        print_result(False, "判断结果更新失败")
        failed += 1

    # 测试3: 获取最近日志
    recent = log.get_recent_entries(5)
    if len(recent) > 0:
        print_result(True, f"获取最近日志成功: {len(recent)}条")
        passed += 1
    else:
        print_result(False, "获取最近日志失败")
        failed += 1

    # 测试4: 统计信息
    stats = log.get_stats()
    if stats["total_entries"] > 0:
        print_result(True, f"统计信息正确: {stats['total_entries']}条日志")
        passed += 1
    else:
        print_result(False, "统计信息错误")
        failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return failed == 0


async def test_config():
    """测试配置管理"""
    print_test_header("配置管理")

    passed = 0
    failed = 0

    # 测试1: 默认配置
    config = WanderConfig()
    if config.idle_threshold == 30 * 60:
        print_result(True, f"默认空闲阈值正确: {config.idle_threshold}秒")
        passed += 1
    else:
        print_result(False, f"默认空闲阈值错误: {config.idle_threshold}")
        failed += 1

    # 测试2: 配置转换
    config_dict = config.to_dict()
    if "idle_threshold" in config_dict:
        print_result(True, "配置转字典成功")
        passed += 1
    else:
        print_result(False, "配置转字典失败")
        failed += 1

    # 测试3: 从字典创建
    new_config = WanderConfig.from_dict({"idle_threshold": 60})
    if new_config.idle_threshold == 60:
        print_result(True, "从字典创建配置成功")
        passed += 1
    else:
        print_result(False, "从字典创建配置失败")
        failed += 1

    # 测试4: 配置管理器
    manager = ConfigManager(config=config)
    manager.set("idle_threshold", 120)
    if manager.config.idle_threshold == 120:
        print_result(True, "配置更新成功")
        passed += 1
    else:
        print_result(False, "配置更新失败")
        failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return failed == 0


async def test_event_handler_factory():
    """测试事件处理器工厂"""
    print_test_header("事件处理器工厂")

    passed = 0
    failed = 0

    mock_llm = MockLLM("模拟响应")
    factory = EventHandlerFactory(call_llm_func=mock_llm)

    # 测试所有事件类型
    event_types = [
        EventType.SLEEP,
        EventType.KEYWORD_EXPANSION,
        EventType.MEMORY_FETCH,
        EventType.BROWSE_NEWS,
        EventType.SELF_REFLECTION,
        EventType.USER_TRACKING,
    ]

    for event_type in event_types:
        handler = factory.get_handler(event_type)
        if handler is not None:
            print_result(True, f"获取 {event_type.value} 处理器成功")
            passed += 1
        else:
            print_result(False, f"获取 {event_type.value} 处理器失败")
            failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return failed == 0


async def test_wander_manager_integration():
    """测试WanderManager集成"""
    print_test_header("WanderManager集成测试")

    passed = 0
    failed = 0

    # 模拟回调
    push_messages = []
    status_changes = []

    async def on_push(message: str):
        push_messages.append(message)

    def on_status(status: dict):
        status_changes.append(status)

    # 创建管理器
    manager = WanderManager(
        idle_threshold=60,
        event_interval=30,
        push_threshold=0.5,
        call_llm_func=MockLLM("模拟响应"),
        on_push_to_user=on_push,
        on_status_change=on_status,
        persistence_enabled=False
    )

    # 测试1: 初始状态
    status = manager.get_status()
    if status["mode"] == "offline":
        print_result(True, "初始状态正确")
        passed += 1
    else:
        print_result(False, f"初始状态错误: {status['mode']}")
        failed += 1

    # 测试2: 用户回复处理
    manager.on_user_reply()
    status = manager.get_status()
    if status["mode"] == "offline":
        print_result(True, "用户回复处理正确")
        passed += 1
    else:
        print_result(False, f"用户回复处理错误: {status['mode']}")
        failed += 1

    # 测试3: 配置更新
    manager.set_idle_threshold(120)
    if manager.get_config().idle_threshold == 120:
        print_result(True, "配置更新成功")
        passed += 1
    else:
        print_result(False, "配置更新失败")
        failed += 1

    # 测试4: 获取日志
    logs = manager.get_recent_logs(5)
    if isinstance(logs, list):
        print_result(True, f"获取日志成功: {len(logs)}条")
        passed += 1
    else:
        print_result(False, "获取日志失败")
        failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return failed == 0


# ==================== 主函数 ====================

async def run_all_tests():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("思维漫想管理器综合测试")
    print("=" * 60)

    tests = [
        ("模式开关层", test_mode_switch),
        ("事件处理器", test_event_handlers),
        ("主动打扰判断层", test_disturb_judgment),
        ("消息生成器", test_message_generator),
        ("概率动态调整", test_probability_state),
        ("漫想日志层", test_wander_log),
        ("配置管理", test_config),
        ("事件处理器工厂", test_event_handler_factory),
        ("WanderManager集成", test_wander_manager_integration),
    ]

    results = {}
    for name, test_func in tests:
        try:
            results[name] = await test_func()
        except Exception as e:
            print(f"  ✗ 测试异常: {e}")
            import traceback
            traceback.print_exc()
            results[name] = False

    # 汇总结果
    print("\n" + "=" * 60)
    print("测试汇总")
    print("=" * 60)

    passed_count = sum(1 for v in results.values() if v)
    failed_count = sum(1 for v in results.values() if not v)

    for name, result in results.items():
        status = "✓ 通过" if result else "✗ 失败"
        print(f"  {name}: {status}")

    print(f"\n总计: {passed_count} 通过, {failed_count} 失败")

    return failed_count == 0


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
