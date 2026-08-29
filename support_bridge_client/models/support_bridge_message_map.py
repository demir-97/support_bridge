from odoo import fields, models


class SupportBridgeMessageMap(models.Model):
    """Which local mail.message corresponds to which message on the vendor's
    side.

    Needed so a reaction given on either side lands on the right message on
    the other. It also guards against duplicates when push and poll race to
    deliver the same message.
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
