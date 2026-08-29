import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SupportBridgeClientController(http.Controller):

    @http.route('/support_bridge/deliver', type='http', auth='public', methods=['POST'], csrf=False)
    def deliver(self, **kwargs):
        """Where the vendor pushes events when this connection is marked
        Publicly Reachable. Best-effort on their side: if this call fails or
        the address cannot be reached, the ordinary poll closes the gap, so
        nothing here needs a retry or a queue."""
        auth_header = request.httprequest.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return request.make_json_response({'ok': False, 'error': 'invalid_api_key'}, status=401)
        api_key = auth_header[len('Bearer '):].strip()
        connection = request.env['support.bridge.connection'].sudo().search([
            ('api_key', '=', api_key), ('state', '=', 'connected'), ('push_enabled', '=', True),
        ], limit=1)
        if not connection:
            return request.make_json_response({'ok': False, 'error': 'invalid_api_key'}, status=401)
        data = request.get_json_data() or {}
        try:
            with request.env.cr.savepoint():
                # advance_cursor=False: pushes can arrive out of order, and
                # moving the cursor here would lose the ones still in flight.
                connection._deliver_one(data, advance_cursor=False)
        except Exception:
            # A push we cannot apply must not turn into a half-applied
            # transaction and an opaque 500. The next poll picks the event up
            # and settles it for good.
            _logger.exception(
                'support_bridge_client: could not apply incoming event %s (connection %s)',
                data.get('id'), connection.id)
            return request.make_json_response({'ok': False, 'error': 'delivery_failed'}, status=500)
        return request.make_json_response({'ok': True})
