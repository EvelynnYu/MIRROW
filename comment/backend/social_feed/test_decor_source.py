import unittest
from social_feed.decor_source import persisted_gift_subject


class GiftSourceTests(unittest.TestCase):
    identifier = 'notification:social_gift:gift_' + 'a' * 32

    def test_visitor_receipt_does_not_prove_a_core_person_act(self):
        # Even a visitor named AI is not the local actor k.
        for name in ('Peer', 'AI', '站主'):
            fact = f'[共域赠礼事实] 收据时间：2026-10-01T10:00:00+08:00。{name}来到本家共域，自动收到了赠礼「杯子」。'
            result = persisted_gift_subject(self.identifier, 'social_gift', fact)
            self.assertEqual(result['recipient_name'], name)
            self.assertNotIn('core_actor_id', result)

    def test_local_recipient_is_fixed_by_the_producer_header(self):
        for name, actor in (('AI', 'k'), ('站主', 'human')):
            fact = f'[共域赠礼事实] 本家收妥时间：2026-10-01T10:00:00+08:00。{name}在朋友家的共域收到了赠礼「杯子」。'
            self.assertEqual(actor, persisted_gift_subject(self.identifier, 'social_gift', fact)['core_actor_id'])
            self.assertEqual({}, persisted_gift_subject('untrusted', 'social_gift', fact))
            self.assertEqual({}, persisted_gift_subject(self.identifier, 'ambient_listening_observation', fact))
            self.assertEqual({}, persisted_gift_subject(self.identifier, 'social_gift', '有人说' + fact))
