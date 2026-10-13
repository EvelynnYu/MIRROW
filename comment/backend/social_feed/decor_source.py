"""Decode this producer's immutable gift receipt header, never gift-description text."""
import re


def persisted_gift_subject(message_id, event_type, content):
    if event_type != 'social_gift' or not re.fullmatch(r'notification:social_gift:gift_[0-9a-f]{32}', message_id):
        return {}
    text = content.removeprefix('[事件记录] ').removeprefix('[共域赠礼事实] ')
    visitor = re.match(r'^(?:收据时间：[0-9T:+-]+。)?([^\n。]{1,80})来到本家共域，自动收到了赠礼「', text)
    if visitor:
        return {'receipt_kind': 'visitor_receives_automatic_gift', 'recipient_name': visitor[1]}
    local = re.match(r'^(?:本家收妥时间：[0-9T:+-]+。)?(AI|站主)在[^\n]+的共域收到了赠礼「', text)
    if local:
        return {'receipt_kind': 'local_receives_gift', 'recipient_name': local[1],
                'core_actor_id': 'k' if local[1] == 'AI' else 'human'}
    return {}
