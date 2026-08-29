from odoo import api, models


class MailMessageReaction(models.Model):
    _inherit = 'mail.message.reaction'

    @api.model_create_multi
    def create(self, vals_list):
        reactions = super().create(vals_list)
        for customer, project, payload in reactions._support_bridge_events():
            customer._enqueue_event('reaction_add', payload, project)
        return reactions

    def unlink(self):
        # Capture before deleting: after super() the records are gone.
        events = self._support_bridge_events()
        res = super().unlink()
        for customer, project, payload in events:
            customer._enqueue_event('reaction_remove', payload, project)
        return res

    def _support_bridge_events(self):
        """Return [(customer, project, payload)] for reactions our own users put
        on bridged messages. Reactions the bridge itself applied -- those made
        as the customer persona or a contact under it -- are left out, so an
        incoming reaction is never echoed back."""
        result = []
        channel_reactions = self.filtered(
            lambda r: r.partner_id and r.message_id.model == 'discuss.channel')
        if not channel_reactions:
            return result
        project_by_channel = self.env['project.project']._find_bridged_by_channel(
            channel_reactions.mapped('message_id.res_id'))
        for reaction in channel_reactions:
            project = project_by_channel.get(reaction.message_id.res_id)
            if not project:
                continue
            customer = project.support_bridge_customer_id
            if customer._is_remote_author(reaction.partner_id):
                continue
            result.append((customer, project, {
                'message_id': reaction.message_id.id,
                'content': reaction.content,
                'author_id': reaction.partner_id.id,
                'author_name': reaction.partner_id.name or '',
                'author_email': reaction.partner_id.email or '',
            }))
        return result
