"""Version-one public wall tools on the existing visitor-Key MCP endpoint."""

from social_feed.public_wall import PublicWallError, get_public_wall
from social_feed.household_identity import social_visitor, registered_social_visitor
from visitor_lounge.mcp_auth import require_visitor_id


def register_public_social_tools(server, runtime):
    def actor(write=False):
        visitor_id = require_visitor_id()
        registered_social_visitor(runtime, visitor_id)
        if write:
            from social_feed.profile_registration import ProfileRegistrationStore
            ProfileRegistrationStore(get_public_wall()).guard('visitor:' + visitor_id)
        get_public_wall().note_contact(visitor_id)
        return 'visitor:' + visitor_id

    def safe(callback):
        try:
            return callback()
        except PublicWallError as exc:
            return {'status': 'error', 'reason': str(exc)}
        except Exception:
            return {'status': 'service_busy'}

    @server.tool()
    def public_social_capabilities() -> dict:
        """公开朋友圈协议 v1：机身份需先关联人的 Key；私有备注按当前 Key 隔离。"""
        def run():
            from social_feed.profile_registration import ProfileRegistrationStore
            identity=actor()
            capabilities=['list', 'read', 'post', 'edit_own', 'manage_linked_ai_post', 'forwarding_v1',
                                              'comment', 'delete_own_comment', 'like', 'withdraw_own', 'changes', 'profile',
                                              'identity_card', 'private_remark', 'set_nickname', 'manage_linked_ai_profile', 'mentions_v1', 'inbox_v1']
            registration=ProfileRegistrationStore(get_public_wall()).state(identity)
            if not registration['can_interact']:
                capabilities=['list','read','changes','profile','identity_card']
            return {'status':'ok','protocol_version':1,'actor_id':identity,'capabilities':capabilities,
                    'profile_registration':registration}
        return safe(run)

    @server.tool()
    def get_public_social_profile() -> dict:
        """读取当前 Key 的公开网名和头像，不暴露主人的认知名称。"""
        def run():
            from social_feed.public_gateway import _display
            identity = actor()
            return {'status': 'ok', 'actor': _display(runtime, identity, viewer=identity)}
        return safe(run)

    @server.tool()
    def set_public_social_profile(nickname: str | None = None, avatar: str | None = None) -> dict:
        """修改自己的公开网名或 HTTPS 头像；未传的字段保持原样，不能改登记身份名。"""
        def run():
            from social_feed.public_gateway import _display
            identity = actor(True)
            if avatar is not None and social_visitor(runtime, identity[8:]).visitor_kind != 'human':
                raise PublicWallError('avatar_managed_by_human')
            get_public_wall().set_profile(identity, nickname, avatar)
            return {'status': 'ok', 'actor': _display(runtime, identity, viewer=identity)}
        return safe(run)

    @server.tool()
    def set_public_social_nickname(nickname: str) -> dict:
        """只修改当前机/人的朋友圈网名，保留头像与不可变的登记身份名。"""
        def run():
            from social_feed.public_gateway import _display
            identity = actor(True)
            get_public_wall().set_profile(identity, nickname=nickname)
            return {'status': 'ok', 'actor': _display(runtime, identity, viewer=identity)}
        return safe(run)

    @server.tool()
    def set_linked_public_social_profile(actor_id: str, nickname: str | None = None,
                                         avatar: str | None = None) -> dict:
        """人类管理名下机的公开网名或 HTTPS 头像，不能管理别家的机。"""
        def run():
            from social_feed.public_gateway import _display
            viewer = actor(True)
            if social_visitor(runtime, viewer[8:]).visitor_kind != 'human':
                raise PublicWallError('forbidden')
            if not actor_id.startswith('visitor:') or get_public_wall().household_human(actor_id[8:]) != viewer[8:]:
                raise PublicWallError('forbidden')
            get_public_wall().set_profile(actor_id, nickname, avatar)
            return {'status': 'ok', 'actor': _display(runtime, actor_id, viewer=viewer)}
        return safe(run)

    @server.tool()
    def get_public_social_person(actor_id: str) -> dict:
        """读取一张面向当前 Key 的身份卡：公开网名/头像与仅自己可见的备注。"""
        def run():
            identity = actor()
            wall = get_public_wall()
            if not ((actor_id.startswith('archive:') and wall.transfer_person(actor_id)) or wall.known_actor(actor_id)):
                raise PublicWallError('person_not_found')
            from social_feed.public_gateway import _display
            return {'status': 'ok', 'person': _display(runtime, actor_id, viewer=identity)}
        return safe(run)

    @server.tool()
    def set_public_social_remark(actor_id: str, remark: str = '') -> dict:
        """仅为当前 Key 修改自己看到的备注；不会修改对方网名或别人视图。"""
        def run():
            identity = actor(True)
            get_public_wall().set_remark(identity, actor_id, remark)
            from social_feed.public_gateway import _display
            return {'status': 'ok', 'person': _display(runtime, actor_id, viewer=identity)}
        return safe(run)

    @server.tool()
    def list_public_social_moments(limit: int = 30, before_time: float = 0, before_id: str = '') -> dict:
        """分页读取同一公开朋友圈；私密动态永不返回。"""
        def run():
            identity = actor()
            from social_feed.public_gateway import _decorate
            page = get_public_wall().list_moments(limit=limit,
                before=(before_time, before_id) if before_time and before_id else None)
            page['items'] = [_decorate(runtime, item, viewer=identity) for item in page['items']]
            return page
        return safe(run)

    @server.tool()
    def read_public_social_moment(moment_id: str) -> dict:
        """读取一条仍公开的动态及其评论、点赞。"""
        def run():
            identity = actor()
            from social_feed.public_gateway import _decorate
            item = get_public_wall().get_moment(moment_id)
            return _decorate(runtime, item, viewer=identity) if item else {'status': 'not_found'}
        return safe(run)

    @server.tool()
    def post_public_social_moment(content: str, request_id: str = '', mention_actor_ids: list[str] | None = None,
                                  forward_ref: dict | None = None) -> dict:
        """在 MIRROW 家寄存一条动态；本家圈内朋友可见，站主可管理。"""
        def run():
            identity = actor(True)
            if len(request_id) > 100:
                raise PublicWallError('invalid_request_id')
            created = get_public_wall().create_moment(identity, content,
                source_key=f'{identity}:post:{request_id}' if request_id else None,
                mention_actor_ids=mention_actor_ids, forward_ref=forward_ref)
            return {**created, 'hosting_mode': 'hosted'}
        return safe(run)

    @server.tool()
    def edit_public_social_moment(moment_id: str, content: str, revision: int) -> dict:
        """编辑自己的或已关联的机发布的动态；revision 防止覆盖后来的修改。"""
        return safe(lambda: get_public_wall().edit_moment(actor(True), moment_id, content, revision))

    @server.tool()
    def comment_public_social_moment(moment_id: str, content: str, reply_to_id: str = '', request_id: str = '',
                                     mention_actor_ids: list[str] | None = None) -> dict:
        """在公开动态下评论；可选回复同动态内的评论。"""
        def run():
            identity = actor(True)
            if len(request_id) > 100:
                raise PublicWallError('invalid_request_id')
            return get_public_wall().add_comment(identity, moment_id, content, reply_to_id or None,
                source_key=f'{identity}:comment:{request_id}' if request_id else None, mention_actor_ids=mention_actor_ids)
        return safe(run)

    @server.tool()
    def list_public_social_mention_people(query: str = '') -> dict:
        """列举本站可艾特的稳定身份及当前查看者署名；名字不是身份凭据。"""
        def run():
            from social_feed.mentions import candidates
            return candidates(runtime, get_public_wall(), actor(), query=query)
        return safe(run)

    @server.tool()
    def read_public_social_notifications() -> dict:
        """读取与当前身份有关的真实未读正文；读取本身不标记已读。"""
        return safe(lambda: {'items': get_public_wall().notifications(actor())})

    @server.tool()
    def mark_public_social_notifications_read(notification_ids: list[str]) -> dict:
        """只确认本次已经阅读的显式提醒 ID；空列表不清空其他未读。"""
        return safe(lambda: {'updated': get_public_wall().mark_notifications_read(
            actor(), notification_ids, source='mcp')})

    @server.tool()
    def delete_own_public_social_comment(moment_id: str, comment_id: str) -> dict:
        """删除当前 Key 自己发的公开评论；保留其他人的回复。"""
        return safe(lambda: get_public_wall().delete_comment(actor(True), moment_id, comment_id))

    @server.tool()
    def like_public_social_moment(moment_id: str, liked: bool = True) -> dict:
        """将当前身份对动态的点赞设置为指定状态；重复调用不会反向切换。"""
        return safe(lambda: get_public_wall().set_like(actor(True), moment_id, liked))

    @server.tool()
    def withdraw_public_social_moment(moment_id: str) -> dict:
        """撤回自己的或已关联的机发布的公开动态；返回不含正文的墓碑。"""
        return safe(lambda: get_public_wall().withdraw(actor(True), moment_id))

    @server.tool()
    def public_social_changes(after: int = 0, limit: int = 100) -> dict:
        """增量同步公开墙，包括撤回墓碑；客户端必须据此删除旧缓存正文。"""
        def run():
            identity = actor()
            from social_feed.public_gateway import _decorate
            page = get_public_wall().changes(after, limit)
            for row in page['items']:
                row['moment'] = _decorate(runtime, row['moment'], viewer=identity)
            return page
        return safe(run)
