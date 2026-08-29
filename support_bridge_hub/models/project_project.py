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

    support_bridge_shared = fields.Boolean(
        string='Shared with Customer', readonly=True, copy=False,
        help="Turns on when you share the project, and stays on until you stop "
             "sharing. Setting a customer and a team only prepares the project — "
             "nothing reaches them until you press Share.",
    )

    def write(self, vals):
        res = super().write(vals)
        # Yalnızca zaten paylaşılmış projeler için ad tazelenir. Müşteri veya
        # takım alanını doldurmak hiçbir şey paylaşmaz; paylaşım açık bir
        # eylemdir, çünkü bir proje kaydı açmak henüz müşteriye anlatılmaya
        # hazır olmak demek değildir.
        if {'name', 'support_bridge_customer_id'} & set(vals):
            for project in self.filtered('support_bridge_shared'):
                project._sync_support_bridge()
                project.support_bridge_customer_id._enqueue_project_sync()
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
        """Jetonu, kanalı ve kanal adını güncel tutar.

        Müşteriye haber vermek bu fonksiyonun işi DEĞİLDİR. Haber, paylaşımı
        başlatan/durduran eylemin kendisinden gider; buraya bağlansaydı
        "kanal zaten var ve adı da aynı" durumunda hiçbir şey gönderilmez ve
        tekrar paylaşım karşı tarafa hiç yansımazdı."""
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
        """Alt kanalın üyeleri takımdan gelir — kimin göreceğinin tek kaynağı
        helpdesk takımının üye listesidir."""
        self.ensure_one()
        self.sudo().support_bridge_channel_id = self.env['discuss.channel'].sudo().create({
            'name': self._bridge_channel_name(),
            'channel_type': parent.channel_type,
            'parent_channel_id': parent.id,
            'channel_member_ids': [
                (0, 0, {'partner_id': partner.id})
                for partner in self.support_bridge_team_id._bridge_partners()
            ],
        })

    def action_support_bridge_share(self):
        """Projeyi müşteriyle paylaşır — kanalları açar, jetonu üretir ve
        listeyi karşı tarafa gönderir. Paylaşım açık bir eylemdir: proje
        kaydını açmak, onu müşteriye göstermeye hazır olmak demek değildir."""
        for project in self:
            if not project.support_bridge_customer_id:
                raise UserError(_(
                    "Pick the customer this project belongs to before sharing it."))
            if not project.support_bridge_team_id:
                raise UserError(_(
                    "Pick the helpdesk team that will handle this project. The "
                    "team decides who can see the conversation."))
            if project.support_bridge_customer_id.active is False:
                raise UserError(_(
                    "%s is archived, so they cannot receive anything. Restore "
                    "the customer first.", project.support_bridge_customer_id.name))
            project.sudo().support_bridge_shared = True
            project._sync_support_bridge()
            # Her paylasimda gonderilir: ilk paylasimda da, durdurulmus bir
            # projenin tekrar paylasilmasinda da. Kanal zaten duruyor olabilir,
            # bu haber vermemenin gerekcesi degil.
            project.support_bridge_customer_id._enqueue_project_sync()
        return True

    def action_support_bridge_revoke(self):
        """Paylaşımı durdurur. Kanal ve geçmiş iki tarafta da kalır, ama
        müşteriye bunun olduğu açıkça söylenir — sessizce kesmek, karşı tarafı
        mesajlarının gittiği yanılgısında bırakır."""
        for project in self:
            if not project.support_bridge_shared:
                continue
            project.sudo().write({'support_bridge_token': False,
                                  'support_bridge_shared': False})
            project._post_bridge_notice(_(
                "Sharing stopped. %s can no longer send or receive messages "
                "here, and they have been told so in their own channel.",
                project.support_bridge_customer_id.name))
            project.support_bridge_customer_id._enqueue_project_sync()
        return True

    def action_support_bridge_regenerate(self):
        for project in self:
            if not project.support_bridge_shared:
                raise UserError(_('Share this project before rotating its token.'))
            project.sudo().support_bridge_token = secrets.token_urlsafe(24)
            project.support_bridge_customer_id._enqueue_project_sync()
        return True

    def _post_bridge_notice(self, body):
        """Kanalın kendi içine bilgilendirme notu. 'notification' tipinde
        gönderilir; köprü yalnızca 'comment' ilettiği için karşı tarafa
        geçmez."""
        self.ensure_one()
        channel = self.sudo().support_bridge_channel_id
        if channel:
            channel.message_post(body=body, message_type='notification',
                                 subtype_xmlid='mail.mt_note')

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
    def _find_unshared_by_channel(self, channel_ids):
        """{kanal id: proje} — kanalı duran ama artık paylaşılmayan projeler."""
        if not channel_ids:
            return {}
        projects = self.sudo().search([
            ('support_bridge_channel_id', 'in', list(channel_ids)),
            ('support_bridge_shared', '=', False),
        ])
        return {p.support_bridge_channel_id.id: p for p in projects}

    def _warn_not_delivered(self):
        """Paylaşımı durmuş kanala yazıldığında bir kez uyarır. Aynı uyarıyı
        her mesajda tekrarlamak kanalı doldurur, o yüzden son mesaj zaten
        uyarıysa tekrar yazılmaz."""
        self.ensure_one()
        channel = self.sudo().support_bridge_channel_id
        if not channel:
            return
        son = channel.message_ids[:1]
        if son and son.message_type == 'notification':
            return
        self._post_bridge_notice(_(
            "Not delivered — this project is no longer shared with %s. "
            "Press Share on the project to resume the conversation.",
            self.support_bridge_customer_id.name or _('the customer')))

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
