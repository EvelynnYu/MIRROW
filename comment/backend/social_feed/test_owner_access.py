import json

import pytest
from fastapi import HTTPException, Request

from routers.social_feed_router import owner_ticket
from social_feed.owner_access import consume_owner_ticket, issue_owner_ticket


def _request(*, peer='127.0.0.1', host='127.0.0.1:8005', origin='http://127.0.0.1:5173', header='1'):
    pairs = [(b'host', host.encode()), (b'origin', origin.encode()),
             (b'x-mirrow-owner-pair', header.encode())]
    return Request({'type':'http', 'method':'POST', 'path':'/api/social-feed/owner-ticket',
                    'headers':pairs, 'client':(peer, 30000), 'server':('127.0.0.1',8005),
                    'scheme':'http'})


def test_owner_ticket_is_single_use():
    ticket = issue_owner_ticket()
    assert consume_owner_ticket(ticket)
    assert not consume_owner_ticket(ticket)


def test_owner_ticket_only_from_local_mirrow_ui():
    for request in (_request(peer='203.0.113.5'), _request(host='evil.example:8005'),
                    _request(origin='https://evil.example'), _request(header='')):
        with pytest.raises(HTTPException) as error:
            owner_ticket(request)
        assert error.value.status_code == 403
    response = owner_ticket(_request())
    payload = json.loads(response.body)
    assert payload['origin'] == 'https://social.example.invalid'
    assert consume_owner_ticket(payload['ticket'])
