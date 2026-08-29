import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Geçici hatalarda deneme aralığı kademeli olarak açılır; son değer tavandır
# ve pencere dolana kadar o aralıkla sürer. Sabit 5 dakika, günlerce kapalı
# kalan bir sunucuya 30 günde ~8600 istek atardı — bu hem boşuna yük, hem de
# toparlanmaya çalışan sunucuyu döven bir davranış olurdu.
RETRY_BACKOFF_MINUTES = (5, 15, 45, 120, 360)
# Bu süre boyunca denemeye devam edilir. Sunucu bir hafta kapalı kalsa bile
# mesaj, açıldıktan en geç altı saat sonra teslim edilir.
RETRY_WINDOW_DAYS = 30
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
    # Hangi projenin konusmasina ait; jeton gonderim aninda projeden okunur ki
    # bayi jetonu yenilerse kuyrukta bekleyen satirlar da yeni jetonla gitsin.
    project_id = fields.Many2one('support.bridge.project', required=True, ondelete='cascade', index=True)
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
    # Boş olması "bir daha denenmeyecek" demektir: ya sunucu içeriği kalıcı
    # olarak reddetti (4xx), ya da yeniden deneme penceresi doldu. Boş bir
    # tarih, cron'un domain'inde hiçbir zaman eşleşmez.
    next_retry = fields.Datetime(string='Next Retry', copy=False, index='btree_not_null')
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
        ok, error, hub_message_id, status_code = connection.send_message(
            self.project_id.sudo().token, self.body, self.author_name, attachments, skipped,
            author_id=self.author_partner_id.id, author_email=self.author_email)
        if ok:
            self.write({'state': 'sent', 'last_error': False, 'next_retry': False})
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
            # fayda etmez, bu yüzden satır tek seferde bırakılır. Diğer her şey
            # (ağ kesintisi, 5xx, zaman aşımı) geçici sayılır ve pencere dolana
            # kadar giderek seyrekleşen aralıklarla denenmeye devam eder.
            attempts = self.attempts + 1
            self.write({
                'state': 'failed',
                'attempts': attempts,
                'last_error': error,
                'next_retry': self._next_retry_at(attempts, 400 <= status_code < 500),
            })

    def _next_retry_at(self, attempts, permanent):
        """Bir sonraki deneme zamanı; artık denenmeyecekse False."""
        self.ensure_one()
        if permanent:
            return False
        now = fields.Datetime.now()
        if now - (self.create_date or now) > timedelta(days=RETRY_WINDOW_DAYS):
            return False
        step = RETRY_BACKOFF_MINUTES[min(attempts, len(RETRY_BACKOFF_MINUTES)) - 1]
        return now + timedelta(minutes=step)

    @api.model
    def _cron_retry_failed(self):
        now = fields.Datetime.now()
        pending_cutoff = now - timedelta(minutes=PENDING_GRACE_MINUTES)
        # next_retry boş olan satırlar bu karşılaştırmada hiçbir zaman
        # eşleşmez; "bir daha denenmeyecek" durumu böyle ifade edilir.
        outbox_rows = self.search([
            '|',
            '&', ('state', '=', 'failed'), ('next_retry', '<=', now),
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
