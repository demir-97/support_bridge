import logging

from markupsafe import Markup

from odoo import _, fields, http
from odoo.http import request
from odoo.tools.mail import plaintext2html

_logger = logging.getLogger(__name__)

POLL_BATCH_SIZE = 100


def _authenticate(env):
    """Resolve the calling customer from the Authorization header.

    Returns a sudo'd support.bridge.customer, or None.
    """
    auth_header = request.httprequest.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None
    api_key = auth_header[len('Bearer '):].strip()
    if not api_key:
        return None
    return env['support.bridge.customer'].sudo().search(
        [('api_key', '=', api_key), ('active', '=', True)], limit=1)


class SupportBridgeHubController(http.Controller):

    @http.route('/support_bridge/ping', type='http', auth='public', methods=['POST'], csrf=False)
    def ping(self, **kwargs):
        customer = _authenticate(request.env)
        if not customer:
            return request.make_json_response({'ok': False, 'error': 'invalid_api_key'}, status=401)
        data = request.get_json_data() or {}
        customer._update_remote_name(data.get('client_name'))
        customer._update_public_url(data.get('public_url'))
        customer.last_seen = fields.Datetime.now()
        company = request.env.company
        return request.make_json_response({
            'ok': True,
            'hub_name': company.name,
            'customer_name': customer.name,
            # The customer builds its sub-channels from this list. A project whose
            # token was revoked is absent: their channel and history stay, but
            # nothing travels for it any more.
            'projects': customer._serialize_projects(),
        })

    @http.route('/support_bridge/inbound', type='http', auth='public', methods=['POST'], csrf=False)
    def inbound(self, **kwargs):
        customer = _authenticate(request.env)
        if not customer:
            return request.make_json_response({'ok': False, 'error': 'invalid_api_key'}, status=401)
        data = request.get_json_data() or {}
        project = request.env['project.project']._find_by_bridge_token(
            customer, data.get('project_token'))
        if not project:
            return request.make_json_response(
                {'ok': False, 'error': 'unknown_project'}, status=404)
        text = (data.get('text') or '').strip()
        attachment_tuples = customer._decode_attachments(data.get('attachments'))
        if not text and not attachment_tuples:
            return request.make_json_response({'ok': False, 'error': 'empty_message'}, status=400)
        author_partner = customer._get_or_create_remote_author(
            data.get('author_name'),
            remote_id=data.get('author_id'),
            email=data.get('author_email'),
        )
        body = plaintext2html(text) if text else ''
        # Attachments the sender could not deliver must be visible here too,
        # otherwise the agent acts on incomplete information.
        skipped = [str(name) for name in (data.get('skipped_attachments') or [])]
        if skipped:
            body += Markup('<p><em>%s</em></p>') % _(
                "Attachment not delivered (size limit): %s", ', '.join(skipped))
        message = project.sudo().support_bridge_channel_id.message_post(
            body=body,
            attachments=attachment_tuples,
            author_id=author_partner.id,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
        )
        customer.last_seen = fields.Datetime.now()
        return request.make_json_response({'ok': True, 'hub_message_id': message.id})

    @http.route('/support_bridge/reaction', type='http', auth='public', methods=['POST'], csrf=False)
    def reaction(self, **kwargs):
        customer = _authenticate(request.env)
        if not customer:
            return request.make_json_response({'ok': False, 'error': 'invalid_api_key'}, status=401)
        data = request.get_json_data() or {}
        action = data.get('action')
        content = (data.get('content') or '').strip()
        if action not in ('add', 'remove') or not content:
            return request.make_json_response({'ok': False, 'error': 'invalid_reaction'}, status=400)
        project = request.env['project.project']._find_by_bridge_token(
            customer, data.get('project_token'))
        if not project:
            return request.make_json_response(
                {'ok': False, 'error': 'unknown_project'}, status=404)
        applied = customer._apply_client_reaction(
            project, data.get('message_id'), content, action,
            data.get('author_name'),
            author_remote_id=data.get('author_id'),
            author_email=data.get('author_email'),
        )
        if not applied:
            return request.make_json_response({'ok': False, 'error': 'unknown_message'}, status=404)
        customer.last_seen = fields.Datetime.now()
        return request.make_json_response({'ok': True})

    @http.route('/support_bridge/outbound', type='http', auth='public', methods=['GET'], csrf=False)
    def outbound(self, since=0, **kwargs):
        customer = _authenticate(request.env)
        if not customer:
            return request.make_json_response({'ok': False, 'error': 'invalid_api_key'}, status=401)
        try:
            since = int(since)
        except (TypeError, ValueError):
            since = 0
        customer.last_seen = fields.Datetime.now()
        events = request.env['support.bridge.event'].sudo().search([
            ('customer_id', '=', customer.id),
            ('id', '>', since),
        ], order='id asc', limit=POLL_BATCH_SIZE)
        payload = [customer._serialize_event(e) for e in events]
        return request.make_json_response({'ok': True, 'events': payload})
