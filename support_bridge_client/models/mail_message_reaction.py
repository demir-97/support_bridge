from odoo import api, models


class MailMessageReaction(models.Model):
    _inherit = 'mail.message.reaction'

    @api.model_create_multi
    def create(self, vals_list):
        reactions = super().create(vals_list)
        for connection, project, remote_message_id, payload in reactions._support_bridge_events():
            connection.send_reaction(
                project.sudo().token, remote_message_id, payload['content'], 'add', payload['author_name'],
                author_id=payload['author_id'], author_email=payload['author_email'])
        return reactions

    def unlink(self):
        # Silmeden önce yakala — super() sonrası kayıtlar artık yok.
        events = self._support_bridge_events()
        res = super().unlink()
        for connection, project, remote_message_id, payload in events:
            connection.send_reaction(
                project.sudo().token, remote_message_id, payload['content'], 'remove', payload['author_name'],
                author_id=payload['author_id'], author_email=payload['author_email'])
        return res

    def _support_bridge_events(self):
        """Bu taraftaki kendi kullanıcılarımızın, köprülenmiş mesajlara verdiği
        tepkiler için [(connection, remote_message_id, payload)] döner.
        Köprünün kendi uyguladığı tepkiler (tedarikçi temsil kontağı veya onun
        kişi kontakları adına olanlar) dışarıda bırakılır; böylece dışarıdan
        gelen bir tepki asla geri yansıtılmaz. Sunucu tarafında karşılığı
        olmayan (hiç köprülenmemiş) mesajlara verilen tepkiler atlanır."""
        result = []
        channel_reactions = self.filtered(
            lambda r: r.partner_id and r.message_id.model == 'discuss.channel')
        if not channel_reactions:
            return result
        projects = self.env['support.bridge.project'].sudo().search([
            ('channel_id', 'in', channel_reactions.mapped('message_id.res_id')),
            ('connection_id.state', '=', 'connected'),
        ])
        project_by_channel = {p.channel_id.id: p for p in projects}
        map_model = self.env['support.bridge.message.map'].sudo()
        for reaction in channel_reactions:
            project = project_by_channel.get(reaction.message_id.res_id)
            if not project:
                continue
            connection = project.connection_id
            if connection._is_remote_author(reaction.partner_id):
                continue
            map_row = map_model.search([
                ('connection_id', '=', connection.id),
                ('local_message_id', '=', reaction.message_id.id),
            ], limit=1)
            if not map_row:
                continue
            result.append((connection, project, map_row.remote_message_id, {
                'content': reaction.content,
                'author_id': reaction.partner_id.id,
                'author_name': reaction.partner_id.name or '',
                'author_email': reaction.partner_id.email or '',
            }))
        return result
