import base64
import logging
import secrets
import threading

import requests

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

PUSH_TIMEOUT = 5
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 20


def _fire_and_forget_post(url, headers, payload, timeout):
    """İsteği daemon thread içinde gönderir; böylece temsilcinin kendi
    transaction'ı, müşterinin sunucusu yavaş veya kapalı olduğunda beklemez.
    Yalnızca garantili bir yedeği olan (müşterinin poll cron'u) en iyi çaba
    gönderimleri için kullanılır; hatalar loglanıp yutulur. Yalnızca gerçekten
    commit edilmiş veri için tetiklenmesi adına cr.postcommit ile
    zamanlanmalıdır."""
    def _send():
        try:
            requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.exceptions.RequestException as e:
            _logger.info('support_bridge_hub: push to %s failed (poll fallback): %s', url, e)
    threading.Thread(target=_send, daemon=True).start()


class SupportBridgeCustomer(models.Model):
    _name = 'support.bridge.customer'
    _description = 'Support Bridge Customer Connection'
    _order = 'name'

    name = fields.Char(
        string='Customer', required=True, default=lambda self: _('New Connection'),
        help="The customer company's display name — replaced automatically with the "
             "customer's own company name the first time they connect.",
    )
    api_key = fields.Char(
        string='API Key',
        default=lambda self: secrets.token_urlsafe(32),
        # index: kimlik doğrulanan her istekte bu alan üzerinden arama yapılır.
        required=True, copy=False, readonly=True, index=True, groups='base.group_system',
        help="Hand this key, together with this server's address, to the customer — "
             "they paste both into their Support Bridge Client settings to connect.",
    )
    partner_id = fields.Many2one(
        'res.partner', string='Customer Contact', readonly=True, copy=False,
        help="Auto-created contact representing the customer's company; relayed "
             "messages are attributed to individual contacts nested under it.",
    )
    channel_id = fields.Many2one(
        # index: her mail.message oluşturmada bu alan üzerinden arama yapılır.
        'discuss.channel', string='Support Channel', readonly=True, copy=False, index=True,
        help="This customer's dedicated Discuss channel, nested under the shared "
             "Support parent channel.",
    )
    agent_user_ids = fields.Many2many(
        'res.users', string='Agents',
        default=lambda self: self.env.user,
        help="Internal users who can see and reply in this customer's support channel.",
    )
    active = fields.Boolean(
        default=True,
        help="Archive a customer to block their access without deleting the chat history.",
    )
    last_seen = fields.Datetime(
        string='Last Contact', readonly=True, copy=False,
        help="Last time the customer's Odoo reached this server.",
    )
    client_public_url = fields.Char(
        string='Customer Public URL', readonly=True, copy=False,
        help="Learned from the customer's own connection settings when they enable "
             "'Publicly Reachable' on their side — used to push replies to them instantly. "
             "Empty means they rely on their own polling only (e.g. behind a firewall).",
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            record._ensure_partner_and_channel()
        return records

    def write(self, vals):
        # Temsilci listesi değişmeden önceki hali yakalanır; hangi kullanıcının
        # listeden düştüğü ancak bu karşılaştırmayla bilinebilir.
        previous = {}
        if 'agent_user_ids' in vals:
            previous = {record.id: record.agent_user_ids for record in self}
        res = super().write(vals)
        if 'agent_user_ids' in vals:
            for record in self:
                record._sync_channel_members(previous.get(record.id))
        return res

    def _ensure_partner_and_channel(self):
        self.ensure_one()
        if not self.partner_id:
            self.partner_id = self.env['res.partner'].sudo().create({
                'name': self.name,
                'is_company': True,
                'comment': _('Support Bridge customer persona — do not merge or delete.'),
            })
        if not self.channel_id:
            parent_channel = self._get_or_create_parent_channel()
            self.channel_id = self.env['discuss.channel'].sudo().create({
                'name': self.name,
                'channel_type': parent_channel.channel_type,
                'parent_channel_id': parent_channel.id,
                'channel_member_ids': [
                    (0, 0, {'partner_id': user.partner_id.id})
                    for user in self.agent_user_ids
                ],
            })

    def _get_or_create_parent_channel(self):
        """Her müşterinin kanalının alt kanal olarak bağlandığı tek üst düzey
        Discuss kanalı. Şirket üzerinde tanımlı olanı kullanır (Ayarlar >
        Kullanıcılar ve Şirketler > Şirketler > Destek Merkezi); tanımlı
        değilse ilk ihtiyaç duyulduğunda oluşturup şirkete kaydeder."""
        company = self.env.company
        parent = company.support_bridge_parent_channel_id
        if not parent:
            parent = self.env['discuss.channel'].sudo().create({
                'name': _('Support'),
                # 'group' tipinde erişim yalnızca üyeliktir (kanalın kendisine ya da # üst kanalına).
                'channel_type': 'group',
                'channel_member_ids': [(0, 0, {'partner_id': self.env.user.partner_id.id})],
            })
            company.sudo().support_bridge_parent_channel_id = parent.id
        return parent

    def _update_remote_name(self, name):
        """Client'tan gelen her /ping isteğinde, client'ın kendi gerçek şirket
        adıyla çağrılır. Böylece alt kanal ve temsil kontağı, API anahtarını
        oluştururken yöneticinin yazdığı geçici ad yerine müşterinin gerçek
        adını taşır."""
        self.ensure_one()
        name = (name or '').strip()
        if not name or name == self.name:
            return
        self.name = name
        if self.partner_id:
            self.partner_id.sudo().name = name
        if self.channel_id:
            self.channel_id.sudo().name = name

    def _update_public_url(self, public_url):
        """Her /ping isteğinde çağrılır — müşterinin anlık teslimat adresini
        kaydeder veya temizler, müşterinin güncel ayarlarıyla eşitler."""
        self.ensure_one()
        public_url = (public_url or '').strip() or False
        if public_url != self.client_public_url:
            self.client_public_url = public_url

    def _enqueue_event(self, event_type, payload):
        """Bu müşteri için bir giden olay kaydeder. Olay kuyruğu, her iki
        teslimat yolunun da okuduğu tek kaynaktır: müşterinin poll cron'u
        kuyruğu olay id'sine göre tarar ve müşteri kendini genel erişilebilir
        ilan ettiyse aynı olay ona anında iletilir."""
        self.ensure_one()
        event = self.env['support.bridge.event'].sudo().create({
            'customer_id': self.id,
            'event_type': event_type,
            'payload': payload,
        })
        self._push_to_client(event)
        return event

    def _serialize_event(self, event):
        """Bir olayın ağ üzerinden gönderilecek biçimi; anlık gönderim ve
        periyodik kontrol aynı biçimi kullanır. Ek içerikleri olay kaydına
        yazılmaz, serileştirme anında tazeden okunur — böylece kuyruk tablosu
        küçük kalır."""
        self.ensure_one()
        data = dict(event.payload or {})
        data['id'] = event.id
        data['type'] = event.event_type
        if event.event_type == 'message' and data.get('message_id'):
            message = self.env['mail.message'].sudo().browse(data['message_id']).exists()
            if message:
                data['attachments'] = self._serialize_attachments(message.attachment_ids)
                # Alıcı da hangi ekin gelmediğini görmeli; aksi halde eksik
                # bilgiye dayanarak hareket eder.
                data['skipped_attachments'] = self._partition_attachments(
                    message.attachment_ids)[1]
            else:
                data['attachments'] = []
        return data

    @api.model
    def _partition_attachments(self, attachments):
        """(iletilecek ekler, iletilemeyeceklerin etiketleri).

        Boyut ve adet sınırlarının tek kaynağı burasıdır. Yalnızca meta veriye
        bakar, ek içeriğini okumaz — bu sayede yalnızca "ne atlanacak?"
        sorusunu cevaplamak için çağrılması ucuzdur.
        """
        keep = self.env['ir.attachment']
        skipped = []
        for attachment in attachments:
            if len(keep) >= MAX_ATTACHMENTS_PER_MESSAGE:
                skipped.append(attachment.name or 'file')
            elif attachment.file_size and attachment.file_size > MAX_ATTACHMENT_BYTES:
                skipped.append('%s (%.0f MB)' % (
                    attachment.name or 'file', attachment.file_size / 1048576))
            else:
                keep |= attachment
        return keep, skipped

    @api.model
    def _serialize_attachments(self, attachments):
        keep, _skipped = self._partition_attachments(attachments)
        result = []
        for attachment in keep:
            attachment = attachment.sudo()
            datas = attachment.datas
            if not datas:
                continue
            result.append({
                'name': attachment.name or 'file',
                'mimetype': attachment.mimetype or '',
                'datas': datas.decode(),
            })
        return result

    def _warn_skipped_attachments(self, skipped):
        """Gönderene, hangi eklerin karşı tarafa gitmediğini kanalın içinde
        söyler. Sessizce atlamak kullanıcıyı dosyanın ulaştığı yanılgısında
        bırakır. 'notification' tipinde gönderilir; köprü yalnızca 'comment'
        tipini ilettiği için bu uyarı karşı tarafa geçmez."""
        self.ensure_one()
        if not skipped or not self.channel_id:
            return
        self.channel_id.sudo().message_post(
            body=_(
                "Not delivered to %(customer)s — attachments are limited to "
                "%(size)s MB per file and %(count)s files per message: %(names)s. "
                "Send a download link instead.",
                customer=self.name,
                size=MAX_ATTACHMENT_BYTES // 1048576,
                count=MAX_ATTACHMENTS_PER_MESSAGE,
                names=', '.join(skipped),
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    @api.model
    def _decode_attachments(self, items):
        """Ağ biçimini message_post'un beklediği `attachments` demetlerine
        çevirir."""
        result = []
        for item in (items or [])[:MAX_ATTACHMENTS_PER_MESSAGE]:
            name = item.get('name') or 'file'
            try:
                raw = base64.b64decode(item.get('datas') or '')
            except Exception:
                # Gönderen taraf zaten sınırları uyguluyor; buraya düşen bir ek
                # bozuk ya da kurcalanmış demektir — sessizce yutulmamalı.
                _logger.warning('support_bridge_hub: ek çözülemedi, atlandı: %s', name)
                continue
            if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
                _logger.warning('support_bridge_hub: ek reddedildi (boş veya sınır aşımı): %s', name)
                continue
            result.append((name, raw))
        return result

    def _apply_client_reaction(self, message_id, content, action, author_name,
                               author_remote_id=None, author_email=None):
        """Müşteri tarafından iletilen tepkiyi, bu müşterinin kanalındaki ilgili
        mesaja, tepkiyi veren kişinin kontağı adına uygular. `_message_reaction`
        hem kaydı değiştirir hem de canlı bus bildirimini gönderir; böylece
        temsilciler tepkinin eklenip kaldırılmasını anında görür."""
        self.ensure_one()
        message = self.env['mail.message'].sudo().browse(int(message_id or 0)).exists()
        if not message or message.model != 'discuss.channel' or message.res_id != self.channel_id.id:
            return False
        author_partner = self._get_or_create_remote_author(
            author_name, remote_id=author_remote_id, email=author_email)
        message._message_reaction(
            content, action,
            partner=author_partner,
            guest=self.env['mail.guest'].sudo().browse(),
        )
        return True

    def _push_to_client(self, event):
        """Kendini genel erişilebilir ilan eden müşteriler için en iyi çaba
        prensibiyle anlık teslimat; commit sonrasında ve ayrı thread'de
        tetiklenir, böylece temsilci müşterinin ağını beklemez. Kuyruğa alma
        veya yeniden deneme yoktur — herhangi bir sebeple başarısız olursa
        müşterinin kendi poll cron'u garantili yedektir, bu yüzden sessizce
        başarısız olmak doğru davranıştır."""
        self.ensure_one()
        if not self.client_public_url:
            return
        url = self.client_public_url + '/support_bridge/deliver'
        headers = {'Authorization': 'Bearer %s' % self.api_key}
        # Şimdi, transaction içindeyken serileştirilir (ek içerikleri dahil);
        # callback'in kendisi commit sonrasında ORM'e dokunmamalıdır.
        payload = self._serialize_event(event)
        self.env.cr.postcommit.add(
            lambda: _fire_and_forget_post(url, headers, payload, PUSH_TIMEOUT))

    def _get_or_create_remote_author(self, name, remote_id=None, email=None):
        """Müşteri tarafındaki gerçek mesaj yazarının burada kullanıcı hesabı
        yoktur. Bu yüzden iletilen mesajlar, müşterinin temsil kontağının
        altında açılan kişiye özel küçük kontaklara atfedilir — temsil
        kontağının kendisine değil; böylece farklı kişiler Discuss'ta kendi
        adlarıyla görünür.

        Eşleştirme anahtarı karşı taraftaki partner id'sidir; ad yalnızca
        görünen etikettir ve karşı tarafta değiştiğinde burada da güncellenir.
        Ada göre eşleştirmek iki hataya yol açardı: aynı adlı iki kişi tek
        kontağa düşer, ad değiştiren kişi ise ikinci bir kontak açtırıp
        geçmişini bölerdi. E-posta da taşınır ama yalnızca ayırt edici bilgi
        olarak — asla eşleştirme anahtarı değildir, çünkü değişebilir ve
        paylaşılan kutular birden çok kişiye ait olabilir.
        """
        self.ensure_one()
        name = (name or '').strip()
        email = (email or '').strip()
        Partner = self.env['res.partner'].sudo()
        domain = [('parent_id', '=', self.partner_id.id)]
        contact = Partner.browse()

        if remote_id:
            contact = Partner.search(
                domain + [('support_bridge_hub_remote_id', '=', remote_id)], limit=1)
            if not contact and name:
                # Kimlik taşımayan eski kontaklar (bu özellik öncesinde ada
                # göre açılmış olanlar) ilk mesajda kimliğiyle eşleştirilir.
                contact = Partner.search(
                    domain + [('support_bridge_hub_remote_id', '=', False),
                              ('name', '=', name)], limit=1)
                if contact:
                    contact.support_bridge_hub_remote_id = remote_id
        elif name:
            # Karşı taraf henüz kimlik göndermiyor (eski sürüm) — ada düş.
            contact = Partner.search(
                domain + [('name', '=', name)], limit=1)

        if contact:
            values = {}
            if name and contact.name != name:
                values['name'] = name
            if email and contact.email != email:
                values['email'] = email
            if values:
                contact.write(values)
            return contact

        if not name and not remote_id:
            return self.partner_id
        return Partner.create({
            'name': name or _('Unknown'),
            'parent_id': self.partner_id.id,
            'email': email or False,
            'support_bridge_hub_remote_id': remote_id or False,
            'comment': _('Support Bridge remote contact — auto-created to represent a real person on the customer side.'),
        })

    def _is_remote_author(self, partner):
        """`partner`, bu müşterinin temsil kontağı ya da onun altında
        _get_or_create_remote_author tarafından açılmış kişi kontaklarından
        biriyse True döner."""
        self.ensure_one()
        return bool(partner) and (partner.id == self.partner_id.id or partner.parent_id.id == self.partner_id.id)

    def _sync_channel_members(self, previous_users=None):
        """Temsilci listesini kanal üyeliğiyle eşitler.

        Kanal 'group' tipinde olduğu için erişim doğrudan üyeliğe bağlıdır —
        listeden çıkarılan bir temsilcinin üyeliği de gerçekten kaldırılmalı,
        aksi halde müşterinin sohbetini görmeye devam eder. Yalnızca listeden
        düşenler çıkarılır; Discuss üzerinden elle davet edilmiş kişilere
        dokunulmaz.
        """
        self.ensure_one()
        if not self.channel_id:
            return
        channel = self.channel_id.sudo()
        to_add = self.agent_user_ids.partner_id - channel.channel_member_ids.partner_id
        if to_add:
            channel.add_members(partner_ids=to_add.ids)
        if previous_users is None:
            return
        parent = channel.parent_channel_id
        for user in previous_users - self.agent_user_ids:
            partner = user.partner_id
            if partner in channel.channel_member_ids.partner_id:
                channel._action_unfollow(partner=partner, post_leave_message=False)
            # Odoo çekirdeği, bir alt kanala eklenen herkesi otomatik olarak üst
            # kanala da üye yapar (discuss.channel.member.create). Üst kanal
            # üyeliği ise tek başına bütün alt kanalları okuma yetkisi verdiği
            # için, yalnızca alt kanaldan çıkarmak erişimi kesmez. Başka hiçbir
            # müşteride temsilci kalmayan kişi üst kanaldan da çıkarılmalıdır.
            if not parent or self.search_count([
                    ('id', '!=', self.id), ('agent_user_ids', 'in', user.id)]):
                continue
            if partner in parent.channel_member_ids.partner_id:
                parent._action_unfollow(partner=partner, post_leave_message=False)

    def action_open_channel(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.channel_id.name,
            'res_model': 'discuss.channel',
            'res_id': self.channel_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
