from odoo import fields, models


class SupportBridgeMessageMap(models.Model):
    """Hangi yerel mail.message'ın sunucu tarafındaki hangi mesaja karşılık
    geldiğini tutar.

    İki taraftan birinde verilen tepkinin karşı tarafta doğru mesaja
    uygulanabilmesi için gereklidir. Ayrıca anlık gönderim ile periyodik
    kontrol aynı mesajı iletmek için yarıştığında mükerrer kaydı önleyen
    koruma görevini görür.
    """
    _name = 'support.bridge.message.map'
    _description = 'Support Bridge Local/Remote Message Map'

    connection_id = fields.Many2one(
        'support.bridge.connection', required=True, ondelete='cascade', index=True)
    local_message_id = fields.Many2one(
        'mail.message', required=True, ondelete='cascade', index=True)
    remote_message_id = fields.Integer(required=True, index=True)

    _remote_unique = models.UniqueIndex("(connection_id, remote_message_id)")
    _local_unique = models.UniqueIndex("(connection_id, local_message_id)")
