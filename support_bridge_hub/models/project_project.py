import secrets

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ProjectProject(models.Model):
    _inherit = 'project.project'

    support_bridge_customer_id = fields.Many2one(
        'support.bridge.customer', string='Support Bridge Customer',
        copy=False, index='btree_not_null',
        help="Which connected customer this project's support conversation "
             "belongs to. Setting it opens a dedicated chat channel for this "
             "project on both sides.",
    )
    support_bridge_team_id = fields.Many2one(
        'helpdesk.team', string='Helpdesk Team', copy=False,
        help="The team that handles support for this project. Every project on "
             "the same team shares one Discuss group, and each project is a "
             "sub-channel under it.",
    )
    support_bridge_token = fields.Char(
        string='Project Token', copy=False, readonly=True,
        index='btree_not_null', groups='base.group_system',
        help="Identifies this project in messages exchanged with the customer. "
             "Revoking it stops this project's conversation without touching "
             "the customer's other projects or their connection.",
    )
    support_bridge_channel_id = fields.Many2one(
        'discuss.channel', string='Support Channel', readonly=True, copy=False,
        index='btree_not_null',
        help="This project's dedicated Discuss sub-channel, nested under the "
             "helpdesk team's group.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        projects = super().create(vals_list)
        for project in projects:
            project._sync_support_bridge()
        return projects

    def write(self, vals):
        res = super().write(vals)
        # Kanal ancak müşteri ve takım birlikte belliyken kurulabilir; ikisinden
        # biri sonradan doldurulduğunda da yakalanmalı, o yüzden her ikisi de
        # tetikleyici. Ad değişikliği alt kanal adına yansır.
        if {'support_bridge_customer_id', 'support_bridge_team_id',
                'name', 'partner_id'} & set(vals):
            for project in self:
                project._sync_support_bridge()
        return res

    def _bridge_channel_name(self):
        """Alt kanal adı: 'Müşteri — Proje'. Müşteri adı öne alınır çünkü
        gruplama takıma göre yapılıyor; aynı grupta farklı müşterilerin
        projeleri yan yana durur ve hangisinin kime ait olduğu ancak adından
        okunabilir."""
        self.ensure_one()
        customer = self.support_bridge_customer_id
        if not customer:
            return self.name or _('Project')
        return '%s — %s' % (customer.name, self.name or _('Project'))

    def _sync_support_bridge(self):
        """Köprüye bağlı proje için jetonu, kanalı ve adı güncel tutar."""
        self.ensure_one()
        if not self.support_bridge_customer_id or not self.support_bridge_team_id:
            return
        if not self.sudo().support_bridge_token:
            self.sudo().support_bridge_token = secrets.token_urlsafe(24)
        parent = self.support_bridge_team_id._get_or_create_bridge_channel()
        channel = self.support_bridge_channel_id.sudo()
        if not channel:
            self._create_bridge_channel(parent)
            return
        if channel.parent_channel_id != parent:
            # Odoo, bir alt kanalın üst kanalını sonradan değiştirmeye izin
            # vermez. Henüz konuşulmamışsa kanalı yeniden kurmak bedelsizdir;
            # geçmiş varsa onu ikiye bölmektense değişikliği reddediyoruz.
            if channel.message_ids.filtered(lambda m: m.message_type == 'comment'):
                raise UserError(_(
                    "This project's conversation already has messages under "
                    "%(old)s, and Odoo cannot move a sub-channel to another "
                    "group. Either keep the current team, or revoke this "
                    "project and link it again to start a fresh channel "
                    "under %(new)s.",
                    old=channel.parent_channel_id.name, new=parent.name))
            channel.unlink()
            self._create_bridge_channel(parent)
            return
        name = self._bridge_channel_name()
        if channel.name != name:
            channel.name = name

    def _create_bridge_channel(self, parent):
        self.ensure_one()
        self.sudo().support_bridge_channel_id = self.env['discuss.channel'].sudo().create({
            'name': self._bridge_channel_name(),
            'channel_type': parent.channel_type,
            'parent_channel_id': parent.id,
            'channel_member_ids': [
                (0, 0, {'partner_id': partner.id})
                for partner in self.support_bridge_customer_id.agent_user_ids.partner_id
            ],
        })

    def action_support_bridge_revoke(self):
        """Jetonu siler: bu projenin sohbeti durur, müşterinin bağlantısı ve
        diğer projeleri etkilenmez. Kanal ve geçmiş olduğu gibi kalır."""
        for project in self:
            project.sudo().support_bridge_token = False
        return True

    def action_support_bridge_regenerate(self):
        for project in self:
            if not project.support_bridge_customer_id:
                raise UserError(_(
                    'Link this project to a Support Bridge customer first.'))
            project.sudo().support_bridge_token = secrets.token_urlsafe(24)
        return True

    @api.model
    def _find_bridged_by_channel(self, channel_ids):
        """{kanal id: proje} — yalnızca jetonu duran projeler. Jetonu iptal
        edilmiş bir projenin kanalına yazılan mesaj dışarı çıkmaz."""
        if not channel_ids:
            return {}
        projects = self.sudo().search([
            ('support_bridge_channel_id', 'in', list(channel_ids)),
            ('support_bridge_token', '!=', False),
            ('support_bridge_customer_id', '!=', False),
        ])
        return {p.support_bridge_channel_id.id: p for p in projects}

    @api.model
    def _find_by_bridge_token(self, customer, token):
        """Gelen bir olayın hangi projeye ait olduğunu bulur. Arama daima
        müşteriyle sınırlanır: jeton tahmin edilse bile başka bir müşterinin
        projesine yazılamaz."""
        token = (token or '').strip()
        if not token or not customer:
            return self.browse()
        return self.sudo().search([
            ('support_bridge_customer_id', '=', customer.id),
            ('support_bridge_token', '=', token),
        ], limit=1)
