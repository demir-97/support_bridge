import logging
import threading

from odoo import SUPERUSER_ID, api, models
from odoo.modules.registry import Registry
from odoo.tools.mail import html2plaintext

_logger = logging.getLogger(__name__)


def _flush_outbox_after_commit(dbname, outbox_ids):
    """Try to send newly queued rows at once, in a daemon thread with its own
    cursor, so writing a chat message never blocks on a slow or unreachable
    vendor. Scheduled with cr.postcommit, so it only runs for rows that
    actually committed; whatever this thread cannot send, the retry cron
    takes over."""
    def _run():
        try:
            registry = Registry(dbname)
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                for row in env['support.bridge.outbox'].browse(outbox_ids).exists():
                    if row.state == 'pending':
                        row._try_send()
        except Exception:
            _logger.exception('support_bridge_client: background outbox flush failed')
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
        # Routing is by project sub-channel: several projects can run with
        # one vendor and each has its own channel. Archived projects are left
        # out, so revoking a token stops the outbound path too.
        projects = self.env['support.bridge.project'].sudo().search([
            ('channel_id', 'in', channel_messages.mapped('res_id')),
            ('connection_id.state', '=', 'connected'),
        ])
        project_by_channel = {p.channel_id.id: p for p in projects}
        # An archived project keeps its channel; whoever writes there must
        # not be left thinking the message went out.
        stale_by_channel = {
            p.channel_id.id: p
            for p in self.env['support.bridge.project'].sudo()
            .with_context(active_test=False).search([
                ('channel_id', 'in', channel_messages.mapped('res_id')),
                ('active', '=', False)])}
        if not project_by_channel and not stale_by_channel:
            return
        outbox_ids = []
        for message in channel_messages:
            stale = stale_by_channel.get(message.res_id)
            if stale:
                stale._warn_not_delivered()
                continue
            project = project_by_channel.get(message.res_id)
            if not project:
                continue
            connection = project.connection_id
            if connection._is_remote_author(message.author_id):
                continue  # relayed in from the vendor -- never echo it back
            body = html2plaintext(message.body or '').strip()
            if not body and not message.attachment_ids:
                continue
            outbox = self.env['support.bridge.outbox'].sudo().create({
                'connection_id': connection.id,
                'project_id': project.id,
                'message_id': message.id,
                # Identity is the partner id; name and email are display only.
                'author_partner_id': message.author_id.id,
                'author_name': message.author_id.name or message.email_from or '',
                'author_email': message.author_id.email or message.email_from or '',
                'body': body,
            })
            outbox_ids.append(outbox.id)
            # Oversized attachments are not delivered; the sender must know.
            skipped = connection._partition_attachments(message.attachment_ids)[1]
            if skipped:
                connection._warn_skipped_attachments(project.channel_id, skipped)
        if outbox_ids:
            dbname = self.env.cr.dbname
            self.env.cr.postcommit.add(
                lambda: _flush_outbox_after_commit(dbname, outbox_ids))
