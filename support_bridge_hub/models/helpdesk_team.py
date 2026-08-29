from odoo import fields, models


class HelpdeskTeam(models.Model):
    _inherit = 'helpdesk.team'

    support_bridge_channel_id = fields.Many2one(
        'discuss.channel', string='Support Bridge Channel',
        readonly=True, copy=False, index='btree_not_null',
        help="The Discuss group that holds this team's customer conversations. "
             "Each bridged project appears as a sub-channel under it. Created "
             "the first time a project on this team is bridged.",
    )

    def _get_or_create_bridge_channel(self):
        """Takımın grup kanalı; proje alt kanalları bunun altına asılır.

        'group' tipi bilinçli: 'channel' tipinde Odoo'nun kayıt kuralı üyeliğe
        bakmadan tüm dahili kullanıcılara okuma izni verir. 'group' tipinde
        erişim yalnızca üyeliktir — kanalın kendisine ya da üst kanalına."""
        self.ensure_one()
        if self.support_bridge_channel_id:
            return self.support_bridge_channel_id
        members = self.member_ids.partner_id | self.env.user.partner_id
        channel = self.env['discuss.channel'].sudo().create({
            'name': self.name,
            'channel_type': 'group',
            'channel_member_ids': [(0, 0, {'partner_id': partner.id}) for partner in members],
        })
        self.sudo().support_bridge_channel_id = channel.id
        return channel

    def write(self, vals):
        res = super().write(vals)
        # Takım adı grubun adıdır; ekipteki değişiklik kanal üyeliğine yansır.
        for team in self.filtered('support_bridge_channel_id'):
            channel = team.support_bridge_channel_id.sudo()
            if 'name' in vals:
                channel.name = team.name
            if 'member_ids' in vals:
                missing = team.member_ids.partner_id - channel.channel_member_ids.partner_id
                if missing:
                    channel.add_members(partner_ids=missing.ids)
        return res
