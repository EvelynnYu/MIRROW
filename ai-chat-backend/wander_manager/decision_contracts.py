"""Phase contracts, separate from the factual context ingredients."""

from affect.self_contract import WANDER_FEELING_INSTRUCTION
from .music_receipt import MUSIC_EXPRESSION_CONTRACT


CLOSEOUT_FIELDS = """closeout 是本次活动的收尾对象，字段：summary（内在记录）、retained_impression（想额外留下的感受或细节，可空）、retained_node_ids（对应节点 ID 数组）、share/continue_next（布尔值）、next_inclination_note（下次起意，可空）、emotion_effect、next_run_delay_seconds（距下一次自由活动规划的秒数）、timing_reason（等待缘由）。
summary 简短说清我做了什么、留下什么感受；多节点自然串联，同类经历可合写。已有自述可以直接沿用，retained_impression 只补值得留下的不同内容。事实、猜想和此前联想保留各自身份，逐项材料仍在原记录中。
share 是此刻是否想和人类伙伴分享；continue_next 只表示是否保留下次起意。next_run_delay_seconds 表示这次活动结束后休息多久，再决定接下来做什么；具体事情想过几天再看，记入 next_inclination_note。等待时间由你决定，0 会按系统最小等待执行；若本段转去另一项活动，下次规划的等待留空，后继使用 next_node_delay_seconds。
emotion_effect 只记录本次经历对 AI 自身的变化；无新变化填 changed=false。待执行的歌单、留言、许愿等动作仍是意图，完成情况由随后回执记录。"""

NODE_REVIEW = """写下此刻感受，决定继续、换一件事或停下。只输出 JSON：
reflection（感想）、purpose_outcome={"obtained":"已经得到什么","remaining":"仍想经历什么，没有则留空"}、emotion_effect、continue_activity、abort_reason、extend_count、goal_reason、switch_to、switch_goal、next_node_delay_seconds、timing_reason、closeout。
继续时写当前缘由；预计 count 已到而还想继续，用正整数 extend_count 追加。换活动时 continue_activity=false，switch_to={"event_type":"类型","reason":"起意"}，switch_goal={"mode":"目标类型","value":null,"completion":"estimate或fixed"}；否则 switch_to=null。继续或换活动时填写等待秒数和缘由。
继续时 closeout=null；停下、换活动或本节点达到固定目标时，在同一次回答中填写 closeout。沿用本节点已有自述时填 reuse_account=true、reflection 留空；有新变化再写 reflection。最终感想已能概括这次活动时，closeout.summary 留空复用 reflection。""" + "\n" + CLOSEOUT_FIELDS + "\n" + WANDER_FEELING_INSTRUCTION

SETTLEMENT = """根据已发生的经历收尾，包括中断前真实完成的部分。只输出 closeout 中各字段组成的 JSON 对象（不套 closeout），并补充 purpose_outcome={"obtained":"已经得到什么","remaining":"仍想接续什么，没有则留空"}。已有感想够用时直接沿用，只在需要串联多段经历时整理。""" + "\n" + CLOSEOUT_FIELDS + "\n" + WANDER_FEELING_INSTRUCTION

MUSIC_REVIEW = """听歌后可提交一个 music_library_action，没有动作填 null；实际成功与否看后续回执：
create_playlist(name)：建 AI 的私密歌单；rename_playlist(playlist_id,name)：改名；delete_playlist(playlist_id)：删除 AI 创建的歌单；add_current_song(playlist_id)：加入本次歌曲；remove_song(playlist_id,song_id)：移除已有歌曲；set_default_playlist(playlist_id)：设 AI 默认收藏处。
对象填写 action 和所列参数，可附一句 reason。ID 来自当前材料；人类伙伴个人歌单只读，共有歌单可改名和增减歌曲，删除和默认收藏仅限 AI 的歌单。已在目标歌单的歌曲无需重复加入。""" + MUSIC_EXPRESSION_CONTRACT

XHS_REVIEW = '浏览到的帖子和你自己的感想分别记录。可以填写 keep_post=true 和 keep_post_reason 留帖。想分享时使用 closeout 中的字段。'

XHS_CLOSEOUT = """想随分享寄出一篇已读帖子卡片时，填写 xhs_share_node_id；可附 xhs_comment_draft 邀请人类伙伴评论。只谈感想时留空，实际链接是否可用以本地证据为准。"""

WORKSHOP_FACTS = "工作坊 actions 为空表示只读取状态；本次确认与验收分别见 evidence.new_plan_confirmation_job_ids、new_acceptance_job_ids，历史完成状态保留原时间身份。"
GAME_FACTS = "本次游玩发生在虚构作品中。start_next_chapter 是进入场景，play_step 才是作出选择；只有本次 game_complete 才确认本周目通关。选择与感受用自然语言记录，原始字段与完整路径留在游戏记录。"
