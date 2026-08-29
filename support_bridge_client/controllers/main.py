import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SupportBridgeClientController(http.Controller):

    @http.route('/support_bridge/deliver', type='http', auth='public', methods=['POST'], csrf=False)
    def deliver(self, **kwargs):
        """Bu bağlantı Genel Erişilebilir olarak ayarlandığında sunucunun
        anlık teslimat için çağırdığı uç nokta. Sunucu tarafında en iyi çaba
        prensibiyle çalışır — bu çağrı başarısız olur ya da adrese
        ulaşılamazsa normal periyodik kontrol açığı kapatır, bu yüzden bu
        uçta yeniden deneme veya kuyruk gerekmez."""
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
                # advance_cursor=False: gönderimler sıra dışı varabilir,
                # imleci burada ilerletmek aradaki olayları kalıcı kaybettirir.
                connection._deliver_one(data, advance_cursor=False)
        except Exception:
            # Teslim edilemeyen bir gönderim, yarım uygulanmış bir transaction
            # ile anlaşılmaz bir 500 hatasına dönüşmemeli; olayı kesin olarak
            # bir sonraki turda periyodik kontrolün atlama mantığı çözecek.
            _logger.exception(
                'support_bridge_client: gelen olay %s işlenemedi (bağlantı %s)',
                data.get('id'), connection.id)
            return request.make_json_response({'ok': False, 'error': 'delivery_failed'}, status=500)
        return request.make_json_response({'ok': True})
