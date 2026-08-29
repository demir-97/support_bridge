from odoo import api, models
from odoo.tools.mail import html2plaintext


class MailMessage(models.Model):
    _inherit = 'mail.message'

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        messages._support_bridge_enqueue_outbound()
        return messages

    def _support_bridge_enqueue_outbound(self):
        channel_messages = self.filtered(
            lambda m: m.model == 'discuss.channel' and m.message_type == 'comment')
        if not channel_messages:
            return
        # Routing is per project, not per customer: one customer can run
        # several projects and each has its own sub-channel.
        project_by_channel = self.env['project.project']._find_bridged_by_channel(
            channel_messages.mapped('res_id'))
        # A project that stopped being shared keeps its channel. An agent
        # writing there must not be left thinking the message went out.
        stale_by_channel = self.env['project.project']._find_unshared_by_channel(
            set(channel_messages.mapped('res_id')) - set(project_by_channel))
        for message in channel_messages:
            stale = stale_by_channel.get(message.res_id)
            if stale:
                stale._warn_not_delivered()
                continue
            project = project_by_channel.get(message.res_id)
            if not project:
                continue
            customer = project.support_bridge_customer_id
            if customer._is_remote_author(message.author_id):
                continue
            body = html2plaintext(message.body or '').strip()
            if not body and not message.attachment_ids:
                continue
            customer._enqueue_event('message', {
                'message_id': message.id,
                # Identity is the partner id; name and email are display
                # only. We send name, not display_name: display_name carries
                # the author's own company prefix ("YourCompany, Ali"), which
                # the far side would bake into the contact name and nest again.
                'author_id': message.author_id.id,
                'author_name': message.author_id.name or message.email_from or '',
                'author_email': message.author_id.email or message.email_from or '',
                'body': body,
                'create_date': message.create_date.isoformat() if message.create_date else '',
            }, project)
            # Oversized attachments are not delivered; the agent must know.
            skipped = customer._partition_attachments(message.attachment_ids)[1]
            if skipped:
                customer._warn_skipped_attachments(
                    project.sudo().support_bridge_channel_id, skipped)
