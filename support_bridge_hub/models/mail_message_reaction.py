from odoo import api, models


class MailMessageReaction(models.Model):
    _inherit = 'mail.message.reaction'

    @api.model_create_multi
    def create(self, vals_list):
        reactions = super().create(vals_list)
        for customer, payload in reactions._support_bridge_events():
            customer._enqueue_event('reaction_add', payload)
        return reactions

    def unlink(self):
        # Silmeden önce yakala — super() sonrası kayıtlar artık yok.
        events = self._support_bridge_events()
        res = super().unlink()
        for customer, payload in events:
            customer._enqueue_event('reaction_remove', payload)
        return res

    def _support_bridge_events(self):
        """Bu taraftaki kendi kullanıcılarımızın, köprülenmiş kanal mesajlarına
        verdiği tepkiler için [(customer, payload)] döner. Köprünün kendi
        uyguladığı tepkiler (müşteri temsil kontağı veya onun kişi kontakları
        adına olanlar) dışarıda bırakılır; böylece dışarıdan gelen bir tepki
        asla geri yansıtılmaz."""
        result = []
        channel_reactions = self.filtered(
            lambda r: r.partner_id and r.message_id.model == 'discuss.channel')
        if not channel_reactions:
            return result
        customers = self.env['support.bridge.customer'].sudo().search([
            ('channel_id', 'in', channel_reactions.mapped('message_id.res_id')),
        ])
        customer_by_channel = {c.channel_id.id: c for c in customers}
        for reaction in channel_reactions:
            customer = customer_by_channel.get(reaction.message_id.res_id)
            if not customer or customer._is_remote_author(reaction.partner_id):
                continue
            result.append((customer, {
                'message_id': reaction.message_id.id,
                'content': reaction.content,
                'author_id': reaction.partner_id.id,
                'author_name': reaction.partner_id.name or '',
                'author_email': reaction.partner_id.email or '',
            }))
        return result
