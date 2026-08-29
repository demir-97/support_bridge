from odoo import fields, models


class HelpdeskTeam(models.Model):
    _inherit = 'helpdesk.team'

    support_bridge_channel_id = fields.Many2one(
        'discuss.channel', string='Support Bridge Channel',
        readonly=True, copy=False, index='btree_not_null',
        help="The Discuss group that holds this team's customer conversations. "
             "Each bridged project appears as a sub-channel under it, and the "
             "team's members are exactly who can see them.",
    )

    def _bridge_partners(self):
        """Kanalları görecek kişiler: takımın üyeleri. Erişimin tek kaynağı
        burasıdır — köprüye ayrı bir temsilci listesi tutulmaz, çünkü iki liste
        er geç birbirinden ayrışır ve hangisinin geçerli olduğu belirsizleşir."""
        self.ensure_one()
        return self.member_ids.partner_id

    def _get_or_create_bridge_channel(self):
        """Takımın grup kanalı; proje alt kanalları bunun altına asılır.

        'group' tipi bilinçli: 'channel' tipinde Odoo'nun kayıt kuralı üyeliğe
        bakmadan tüm dahili kullanıcılara okuma izni verir. 'group' tipinde
        erişim yalnızca üyeliktir — kanalın kendisine ya da üst kanalına."""
        self.ensure_one()
        if self.support_bridge_channel_id:
            return self.support_bridge_channel_id
        channel = self.env['discuss.channel'].sudo().create({
            'name': self.name,
            'channel_type': 'group',
            'channel_member_ids': [
                (0, 0, {'partner_id': partner.id}) for partner in self._bridge_partners()],
        })
        self.sudo().support_bridge_channel_id = channel.id
        return channel

    def write(self, vals):
        previous = {}
        if 'member_ids' in vals:
            previous = {team.id: team.member_ids for team in self}
        res = super().write(vals)
        for team in self.filtered('support_bridge_channel_id'):
            if 'name' in vals:
                team.support_bridge_channel_id.sudo().name = team.name
            if 'member_ids' in vals:
                team._sync_bridge_members(previous.get(team.id))
        return res

    def _sync_bridge_members(self, previous_members=None):
        """Takım üyeliğini grup kanalına ve altındaki tüm proje kanallarına
        yansıtır.

        Çıkarma iki yerde birden yapılmalıdır: Odoo bir alt kanala eklenen
        herkesi otomatik olarak üst kanala da üye yapar, üst kanal üyeliği ise
        tek başına bütün alt kanalları okuma yetkisi verir. Yalnızca birinden
        çıkarmak erişimi gerçekten kesmez."""
        self.ensure_one()
        group = self.support_bridge_channel_id.sudo()
        if not group:
            return
        subs = self.env['project.project'].sudo().search(
            [('support_bridge_team_id', '=', self.id)]).support_bridge_channel_id
        partners = self._bridge_partners()
        for channel in group | subs:
            missing = partners - channel.channel_member_ids.partner_id
            if missing:
                channel.add_members(partner_ids=missing.ids)
        if previous_members is None:
            return
        for partner in (previous_members - self.member_ids).partner_id:
            for channel in subs | group:
                if partner in channel.channel_member_ids.partner_id:
                    channel._action_unfollow(partner=partner, post_leave_message=False)
