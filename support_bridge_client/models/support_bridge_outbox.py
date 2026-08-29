import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# Gönderilmiş satırlar yalnızca kayıt tutma amaçlıdır ("sunucuya ulaştı mı?")
# — sohbet mesajlarının kendisi mail.message'ta durur ve asla silinmez.
# Başarısız satırlar, dışarı çıkamayan mesajların kanıtı olarak kalıcı tutulur.
SENT_RETENTION_DAYS = 30
# Yeni oluşturulan satırlar commit sonrası arka plan thread'i tarafından
# gönderilir; cron yalnızca thread'in açıkça hiç çalışmadığı kadar eski
# bekleyen satırları alır (arada sunucu yeniden başlamıştır) — böylece iki yol
# aynı mesajı iki kez göndermez.
PENDING_GRACE_MINUTES = 5


class SupportBridgeOutbox(models.Model):
    _name = 'support.bridge.outbox'
    _description = 'Support Bridge Outbound Message Queue'
    _order = 'id'

    connection_id = fields.Many2one('support.bridge.connection', required=True, ondelete='cascade')
    message_id = fields.Many2one('mail.message', ondelete='set null')
    # Karşı tarafın kontağı bu id ile eşleştirir; ad ve e-posta yalnızca
    # görünen bilgidir. message_id silinebildiği için yazar bilgisi burada
    # ayrıca saklanır.
    author_partner_id = fields.Many2one('res.partner', ondelete='set null')
    author_name = fields.Char()
    author_email = fields.Char()
    body = fields.Text()
    state = fields.Selection(
        [('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed')],
        default='pending', required=True,
    )
    attempts = fields.Integer(default=0)
    last_error = fields.Text()

    def _try_send(self):
        self.ensure_one()
        connection = self.connection_id
        # Ekler, giden kuyruk satırına kopyalanmaz; gönderim anında kaynak
        # mesajdan okunur — böylece kesinti sonrası yeniden denemede de
        # eklere erişilebilir.
        source_attachments = self.message_id.attachment_ids if self.message_id else \
            self.env['ir.attachment']
        attachments = connection._serialize_attachments(source_attachments)
        skipped = connection._partition_attachments(source_attachments)[1]
        ok, error, hub_message_id = connection.send_message(
            self.body, self.author_name, attachments, skipped,
            author_id=self.author_partner_id.id, author_email=self.author_email)
        if ok:
            self.write({'state': 'sent', 'last_error': False})
            if hub_message_id and self.message_id:
                map_model = self.env['support.bridge.message.map'].sudo()
                if not map_model.search_count([
                        ('connection_id', '=', connection.id),
                        ('local_message_id', '=', self.message_id.id)]):
                    map_model.create({
                        'connection_id': connection.id,
                        'local_message_id': self.message_id.id,
                        'remote_message_id': hub_message_id,
                    })
        else:
            # 4xx, sunucunun "bu içerik hiçbir zaman kabul edilmeyecek"
            # demesidir (hatalı anahtar, boş mesaj, ...) — yeniden denemek
            # fayda etmez, bu yüzden boşuna zorlamak yerine tüm deneme hakkı
            # tek seferde tüketilir.
            permanent = isinstance(error, str) and error.startswith('HTTP 4')
            self.write({
                'state': 'failed',
                'attempts': MAX_ATTEMPTS if permanent else self.attempts + 1,
                'last_error': error,
            })

    @api.model
    def _cron_retry_failed(self):
        pending_cutoff = fields.Datetime.now() - timedelta(minutes=PENDING_GRACE_MINUTES)
        outbox_rows = self.search([
            '|',
            '&', ('state', '=', 'failed'), ('attempts', '<', MAX_ATTEMPTS),
            '&', ('state', '=', 'pending'), ('create_date', '<', pending_cutoff),
        ])
        for row in outbox_rows:
            try:
                row._try_send()
            except Exception:
                _logger.exception('support_bridge_client: giden kuyruk satırı %s yeniden denenirken beklenmeyen hata', row.id)

    @api.autovacuum
    def _gc_sent_rows(self):
        cutoff = fields.Datetime.now() - timedelta(days=SENT_RETENTION_DAYS)
        self.search([('state', '=', 'sent'), ('create_date', '<', cutoff)]).unlink()
