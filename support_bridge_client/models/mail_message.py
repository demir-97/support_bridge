import logging
import threading

from odoo import SUPERUSER_ID, api, models
from odoo.modules.registry import Registry
from odoo.tools.mail import html2plaintext

_logger = logging.getLogger(__name__)


def _flush_outbox_after_commit(dbname, outbox_ids):
    """Yeni kuyruğa alınan satırların anlık gönderimini, kendi cursor'una sahip
    bir daemon thread içinde dener; böylece sohbet mesajı yazmak, sunucu yavaş
    veya erişilemez olduğunda beklemeye takılmaz. cr.postcommit ile
    zamanlandığı için yalnızca gerçekten commit edilmiş satırlar için çalışır;
    bu thread'in gönderemediği her şeyi yeniden deneme cron'u üstlenir."""
    def _run():
        try:
            registry = Registry(dbname)
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                for row in env['support.bridge.outbox'].browse(outbox_ids).exists():
                    if row.state == 'pending':
                        row._try_send()
        except Exception:
            _logger.exception('support_bridge_client: arka planda giden kuyruk gönderimi başarısız')
    threading.Thread(target=_run, daemon=True).start()


class MailMessage(models.Model):
    _inherit = 'mail.message'

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        messages._support_bridge_relay_outbound()
        return messages

    def _support_bridge_relay_outbound(self):
        channel_messages = self.filtered(
            lambda m: m.model == 'discuss.channel' and m.message_type == 'comment')
        if not channel_messages:
            return
        # Yönlendirme proje alt kanalına göre: bir bayiyle birden çok proje
        # yürüyebilir ve her birinin kendi kanalı vardır. Arşivlenmiş projeler
        # dışarıda kalır, yani bayi jetonu iptal ettiğinde giden yol da durur.
        projects = self.env['support.bridge.project'].sudo().search([
            ('channel_id', 'in', channel_messages.mapped('res_id')),
            ('connection_id.state', '=', 'connected'),
        ])
        if not projects:
            return
        project_by_channel = {p.channel_id.id: p for p in projects}
        outbox_ids = []
        for message in channel_messages:
            project = project_by_channel.get(message.res_id)
            if not project:
                continue
            connection = project.connection_id
            if connection._is_remote_author(message.author_id):
                continue  # sunucudan iletilmiş mesaj — asla geri yansıtma
            body = html2plaintext(message.body or '').strip()
            if not body and not message.attachment_ids:
                continue
            outbox = self.env['support.bridge.outbox'].sudo().create({
                'connection_id': connection.id,
                'project_id': project.id,
                'message_id': message.id,
                # Kimlik anahtarı partner id'sidir; ad ve e-posta yalnızca görünen bilgidir.
                'author_partner_id': message.author_id.id,
                'author_name': message.author_id.name or message.email_from or '',
                'author_email': message.author_id.email or message.email_from or '',
                'body': body,
            })
            outbox_ids.append(outbox.id)
            # Sınırı aşan ekler iletilmez; gönderenin bunu bilmesi gerekir.
            skipped = connection._partition_attachments(message.attachment_ids)[1]
            if skipped:
                connection._warn_skipped_attachments(project.channel_id, skipped)
        if outbox_ids:
            dbname = self.env.cr.dbname
            self.env.cr.postcommit.add(
                lambda: _flush_outbox_after_commit(dbname, outbox_ids))
