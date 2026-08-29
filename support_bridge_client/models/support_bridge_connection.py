import base64
import logging
import threading
from urllib.parse import urlparse

import requests
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.mail import plaintext2html

_logger = logging.getLogger(__name__)

TIMEOUT = 15
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 20


def _looks_internal(url):
    """Hub'ın internet üzerinden ulaşamayacağı adresleri kabaca ayıklar.
    Yalnızca uyarı üretmek için kullanılır: aynı yerel ağdaki bir hub böyle
    bir adrese pekâlâ erişebilir, o yüzden bu asla engelleyici olmamalı."""
    host = (urlparse(url).hostname or '').lower()
    if host in ('localhost', '::1') or host.endswith('.local'):
        return True
    if host.startswith(('127.', '10.', '192.168.', '169.254.', '0.')):
        return True
    parts = host.split('.')
    if len(parts) == 4 and parts[0] == '172' and parts[1].isdigit():
        return 16 <= int(parts[1]) <= 31
    return False


def _fire_and_forget_post(url, headers, payload, timeout):
    def _send():
        try:
            requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.exceptions.RequestException as e:
            _logger.info('support_bridge_client: arka plan gönderimi başarısız %s: %s', url, e)
    threading.Thread(target=_send, daemon=True).start()


class SupportBridgeConnection(models.Model):
    _name = 'support.bridge.connection'
    _description = 'Support Bridge Connection'

    name = fields.Char(string='Vendor', compute='_compute_name')
    hub_url = fields.Char(
        string='Hub URL', required=True,
        help="The support server address your vendor gave you, "
             "e.g. https://support.yourvendor.com",
    )
    api_key = fields.Char(
        string='API Key', required=True, groups='base.group_system',
        help="Paste the key your vendor generated for this connection.",
    )
    state = fields.Selection(
        [('draft', 'Not Connected'), ('connected', 'Connected'), ('error', 'Connection Error')],
        string='Status', default='draft', readonly=True, copy=False,
    )
    last_error = fields.Char(string='Last Error', readonly=True, copy=False)
    partner_id = fields.Many2one(
        'res.partner', string='Vendor Contact', readonly=True, copy=False,
        help="Auto-created contact representing your vendor; their replies are "
             "attributed to individual contacts nested under it.",
    )
    channel_id = fields.Many2one(
        'discuss.channel', string='Support Channel', readonly=True, copy=False, index=True,
        help="The Discuss channel where you chat with your vendor's support team.",
    )
    member_user_ids = fields.Many2many(
        'res.users', string='Team',
        default=lambda self: self.env.user,
        help="Internal users who can see and use this support channel.",
    )
    last_poll_cursor = fields.Integer(default=0, readonly=True, copy=False)
    last_synced = fields.Datetime(
        string='Last Synchronized', readonly=True, copy=False,
        help="Last time this connection checked the vendor's server for replies.",
    )
    push_enabled = fields.Boolean(
        string='Publicly Reachable', default=False,
        help="Enable only if this Odoo instance has a public URL the vendor's server can "
             "reach directly (e.g. cloud/Odoo.sh hosting) — replies then arrive instantly "
             "instead of waiting for the next check. Leave off if this server is behind a "
             "firewall/NAT; background checking keeps working either way as the reliable "
             "fallback.",
    )
    public_url = fields.Char(
        string='My Public URL',
        help="This instance's own public base URL, e.g. https://mycompany.odoo.com — "
             "required when Publicly Reachable is enabled.")

    @api.depends('hub_url', 'partner_id.name')
    def _compute_name(self):
        for record in self:
            record.name = record.partner_id.name or record.hub_url or _('New Connection')

    @api.onchange('push_enabled')
    def _onchange_push_enabled(self):
        """Kutu işaretlendiğinde adresi Odoo'nun kendi taban URL'i ile doldurur.
        Alan düzenlenebilir kalır ve dolu bir değer asla ezilmez; amaç elle
        yazımdaki hata riskini azaltmak — yanlış adres, mesaj içeriğinin ve API
        anahtarının tanımadığımız bir sunucuya gönderilmesi demektir."""
        if not self.push_enabled or self.public_url:
            return
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url') or ''
        self.public_url = base_url.rstrip('/')
        if not self.public_url:
            return
        if _looks_internal(self.public_url):
            return {'warning': {
                'title': _('Check this address'),
                'message': _(
                    "%s is an internal address, so a vendor outside your network "
                    "cannot reach it. Replace it with the address you use to open "
                    "this Odoo from outside, or untick Publicly Reachable — "
                    "replies still arrive either way.", self.public_url),
            }}
        if self.public_url.startswith('http://'):
            # TLS'i sonlandıran bir proxy arkasında proxy_mode kapalıysa Odoo
            # kendi adresini http sanır. Bu adrese yapılan gönderim genelde
            # https'e yönlendirilir ve yönlendirmede POST düşer.
            return {'warning': {
                'title': _('Use https for this address'),
                'message': _(
                    "%s is not encrypted, so your API key would travel in the "
                    "clear and deliveries may be lost to a redirect. Change it "
                    "to https if your Odoo is reachable that way.", self.public_url),
            }}

    def write(self, vals):
        # Ekip listesi değişmeden önceki hali yakalanır; hangi kullanıcının
        # listeden düştüğü ancak bu karşılaştırmayla bilinebilir.
        previous = {}
        if 'member_user_ids' in vals:
            previous = {record.id: record.member_user_ids for record in self}
        res = super().write(vals)
        if 'member_user_ids' in vals:
            for record in self:
                record._sync_channel_members(previous.get(record.id))
        return res

    def _sync_channel_members(self, previous_users=None):
        """Ekip listesini kanal üyeliğiyle eşitler.

        Kanal 'group' tipinde olduğu için erişim doğrudan üyeliğe bağlıdır —
        listeden çıkarılan bir kullanıcının üyeliği de gerçekten kaldırılmalı,
        aksi halde kanalı görmeye devam eder. Yalnızca listeden düşenler
        çıkarılır; Discuss üzerinden elle davet edilmiş kişilere dokunulmaz.
        """
        self.ensure_one()
        if not self.channel_id:
            return
        channel = self.channel_id.sudo()
        to_add = self.member_user_ids.partner_id - channel.channel_member_ids.partner_id
        if to_add:
            channel.add_members(partner_ids=to_add.ids)
        if previous_users is None:
            return
        dropped = (previous_users - self.member_user_ids).partner_id
        for partner in dropped & channel.channel_member_ids.partner_id:
            channel._action_unfollow(partner=partner, post_leave_message=False)

    def _headers(self):
        self.ensure_one()
        return {'Authorization': 'Bearer %s' % (self.api_key or '')}

    def action_connect(self):
        self.ensure_one()
        if not self.hub_url or not self.api_key:
            raise UserError(_('Please fill in both the Hub URL and the API Key.'))
        if self.push_enabled and not self.public_url:
            raise UserError(_('Please fill in your Public URL, or disable Publicly Reachable.'))
        url = self.hub_url.rstrip('/') + '/support_bridge/ping'
        try:
            response = requests.post(
                url, headers=self._headers(),
                json={
                    'client_name': self.env.company.name,
                    'public_url': self.public_url.rstrip('/') if self.push_enabled and self.public_url else '',
                },
                timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            self.write({'state': 'error', 'last_error': str(e)})
            raise UserError(_('Could not reach the Hub at %(url)s: %(error)s', url=url, error=e))
        if not response.ok:
            self.write({'state': 'error', 'last_error': 'HTTP %s' % response.status_code})
            raise UserError(_('The Hub rejected the connection (HTTP %s). Check the API Key.', response.status_code))
        data = response.json()
        if not data.get('ok'):
            self.write({'state': 'error', 'last_error': data.get('error') or 'unknown_error'})
            raise UserError(_('The Hub rejected the connection: %s', data.get('error')))

        hub_name = data.get('hub_name') or self.hub_url
        if not self.partner_id:
            self.partner_id = self.env['res.partner'].sudo().create({
                'name': hub_name,
                'is_company': True,
                'comment': _('Support Bridge vendor persona — do not merge or delete.'),
            })
        else:
            self.partner_id.sudo().name = hub_name
        if not self.channel_id:
            self.channel_id = self.env['discuss.channel'].sudo().create({
                'name': hub_name,
                'channel_type': 'group',
                'channel_member_ids': [
                    (0, 0, {'partner_id': user.partner_id.id})
                    for user in self.member_user_ids
                ],
            })
        else:
            self.channel_id.sudo().name = hub_name
        self.write({'state': 'connected', 'last_error': False})

    def _get_or_create_remote_author(self, name, remote_id=None, email=None):
        """Sunucu tarafındaki gerçek yanıt yazarının burada kullanıcı hesabı
        yoktur. Bu yüzden iletilen mesajlar, bu bağlantının tedarikçi temsil
        kontağının altında açılan kişiye özel küçük kontaklara atfedilir —
        temsil kontağının kendisine değil; böylece farklı temsilciler
        Discuss'ta kendi adlarıyla görünür.
        """
        self.ensure_one()
        name = (name or '').strip()
        email = (email or '').strip()
        Partner = self.env['res.partner'].sudo()
        domain = [('parent_id', '=', self.partner_id.id)]
        contact = Partner.browse()

        if remote_id:
            contact = Partner.search(
                domain + [('support_bridge_client_remote_id', '=', remote_id)], limit=1)
            if not contact and name:
                # Kimlik taşımayan eski kontaklar (bu özellik öncesinde ada
                # göre açılmış olanlar) ilk mesajda kimliğiyle eşleştirilir.
                contact = Partner.search(
                    domain + [('support_bridge_client_remote_id', '=', False),
                              ('name', '=', name)], limit=1)
                if contact:
                    contact.support_bridge_client_remote_id = remote_id
        elif name:
            # Karşı taraf henüz kimlik göndermiyor ise
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
            'support_bridge_client_remote_id': remote_id or False,
            'comment': _('Support Bridge remote contact — auto-created to represent a real person on the Hub side.'),
        })

    def _is_remote_author(self, partner):
        """`partner`, bu bağlantının tedarikçi temsil kontağı ya da onun
        altında _get_or_create_remote_author tarafından açılmış kişi
        kontaklarından biriyse True döner."""
        self.ensure_one()
        return bool(partner) and (
            partner.id == self.partner_id.id or partner.parent_id.id == self.partner_id.id)

    def send_message(self, text, author_name=None, attachments=None,
                     skipped_attachments=None, author_id=None, author_email=None):
        """Bir mesajı (metin ve/veya ekler) sunucuya gönderir. Asla hata
        fırlatmaz — çağıranlar (giden kuyruk yeniden deneme işi ve mesaj
        oluşturma kancası) sunucuya ulaşılamadığında da yerel olarak
        çalışmaya devam etmelidir.

        (ok, error, hub_message_id) döner."""
        self.ensure_one()
        url = self.hub_url.rstrip('/') + '/support_bridge/inbound'
        try:
            response = requests.post(
                url, headers=self._headers(),
                json={
                    'text': text or '',
                    'author_id': author_id or 0,
                    'author_name': author_name or '',
                    'author_email': author_email or '',
                    'attachments': attachments or [],
                    'skipped_attachments': skipped_attachments or [],
                }, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            return False, str(e), 0
        if not response.ok:
            return False, 'HTTP %s' % response.status_code, 0
        data = response.json()
        if not data.get('ok'):
            return False, data.get('error') or 'unknown_error', 0
        return True, False, data.get('hub_message_id') or 0

    def send_reaction(self, remote_message_id, content, action, author_name=None,
                      author_id=None, author_email=None):
        """Bir emoji tepkisini (ekleme veya kaldırma) sunucuya iletir; commit sonrasında ve ayrı thread'de çalışır."""
        self.ensure_one()
        url = self.hub_url.rstrip('/') + '/support_bridge/reaction'
        headers = self._headers()
        payload = {
            'message_id': remote_message_id,
            'content': content,
            'action': action,
            'author_id': author_id or 0,
            'author_name': author_name or '',
            'author_email': author_email or '',
        }
        self.env.cr.postcommit.add(
            lambda: _fire_and_forget_post(url, headers, payload, TIMEOUT))

    @api.model
    def _partition_attachments(self, attachments):
        """İletilecek ekler, iletilemeyeceklerin etiketleri"""
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
        """ir.attachment kayıtlarını ağ biçimine çevirir (name/mimetype/base64 datas)."""
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
                "Not delivered to %(vendor)s — attachments are limited to "
                "%(size)s MB per file and %(count)s files per message: %(names)s. "
                "Send a download link instead.",
                vendor=self.partner_id.name or self.hub_url,
                size=MAX_ATTACHMENT_BYTES // 1048576,
                count=MAX_ATTACHMENTS_PER_MESSAGE,
                names=', '.join(skipped),
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def _deliver_one(self, item, advance_cursor=True):
        """Sunucudan gelen tek bir olayı yerel olarak işler; olay ister anlık
        gönderimle ister periyodik kontrolle gelmiş olsun aynı yol kullanılır.

        `advance_cursor` yalnızca periyodik kontrol için True olmalıdır. Orada
        olaylar id sırasına göre tek tek işlendiği için imleci ilerletmek
        güvenlidir. Anlık gönderimde ise her olay kendi thread'inde gittiğinden
        olaylar sıra dışı varabilir; imleç orada ilerletilseydi, geç kalan
        küçük id'li bir olay "zaten işlenmiş" sayılıp kalıcı olarak
        kaybolurdu — periyodik kontrol de artık onu istemeyeceği için geri
        gelmezdi. Bu yüzden anlık gönderim imlece hiç dokunmaz; mükerrer
        teslimatı mesaj eşleme tablosu (mesajlar) ve tepkilerin doğası gereği
        yinelenebilir olması (aynı tepkiyi iki kez eklemek tek kayıt üretir)
        engeller.
        """
        self.ensure_one()
        event_id = item.get('id') or 0
        if event_id and event_id <= self.last_poll_cursor:
            return
        event_type = item.get('type') or 'message'
        if event_type == 'message':
            self._deliver_message(item)
        elif event_type in ('reaction_add', 'reaction_remove'):
            self._deliver_reaction(item, 'add' if event_type == 'reaction_add' else 'remove')
        if advance_cursor and event_id > self.last_poll_cursor:
            self.write({'last_poll_cursor': event_id, 'last_synced': fields.Datetime.now()})

    def _deliver_message(self, item):
        self.ensure_one()
        remote_message_id = item.get('message_id') or 0
        map_model = self.env['support.bridge.message.map'].sudo()
        if remote_message_id and map_model.search_count([
                ('connection_id', '=', self.id),
                ('remote_message_id', '=', remote_message_id)]):
            return  # diğer teslimat yolundan zaten iletilmiş
        attachment_tuples = []
        for att in (item.get('attachments') or [])[:MAX_ATTACHMENTS_PER_MESSAGE]:
            name = att.get('name') or 'file'
            try:
                raw = base64.b64decode(att.get('datas') or '')
            except Exception:
                # Gönderen taraf zaten sınırları uyguluyor; buraya düşen bir ek
                # bozuk ya da kurcalanmış demektir — sessizce yutulmamalı.
                _logger.warning('support_bridge_client: ek çözülemedi, atlandı: %s', name)
                continue
            if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
                _logger.warning('support_bridge_client: ek reddedildi (boş veya sınır aşımı): %s', name)
                continue
            attachment_tuples.append((name, raw))
        body = (item.get('body') or '').strip()
        skipped = [str(name) for name in (item.get('skipped_attachments') or [])]
        if not body and not attachment_tuples and not skipped:
            return
        author_partner = self._get_or_create_remote_author(
            item.get('author_name'),
            remote_id=item.get('author_id'),
            email=item.get('author_email'),
        )
        html_body = plaintext2html(body) if body else ''
        # Karşı tarafta sınıra takılan ekler burada da görünür olmalı; aksi
        # halde kullanıcı eksik bilgiye dayanarak hareket eder.
        if skipped:
            html_body += Markup('<p><em>%s</em></p>') % _(
                "Attachment not delivered (size limit): %s", ', '.join(skipped))
        message = self.channel_id.sudo().message_post(
            body=html_body,
            attachments=attachment_tuples,
            author_id=author_partner.id,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
        )
        if remote_message_id:
            map_model.create({
                'connection_id': self.id,
                'local_message_id': message.id,
                'remote_message_id': remote_message_id,
            })

    def _deliver_reaction(self, item, action):
        self.ensure_one()
        map_row = self.env['support.bridge.message.map'].sudo().search([
            ('connection_id', '=', self.id),
            ('remote_message_id', '=', item.get('message_id') or 0),
        ], limit=1)
        if not map_row:
            return  # buraya hiç köprülenmemiş bir mesaja verilen tepki
        content = (item.get('content') or '').strip()
        if not content:
            return
        author_partner = self._get_or_create_remote_author(
            item.get('author_name'),
            remote_id=item.get('author_id'),
            email=item.get('author_email'),
        )
        map_row.local_message_id.sudo()._message_reaction(
            content, action,
            partner=author_partner,
            guest=self.env['mail.guest'].sudo().browse(),
        )

    def _poll_one(self):
        self.ensure_one()
        url = self.hub_url.rstrip('/') + '/support_bridge/outbound'
        try:
            response = requests.get(
                url, headers=self._headers(), params={'since': self.last_poll_cursor},
                timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            _logger.warning('support_bridge_client: kontrol başarısız (bağlantı %s): %s', self.id, e)
            return
        if not response.ok:
            _logger.warning('support_bridge_client: kontrol başarısız (bağlantı %s): HTTP %s',
                             self.id, response.status_code)
            return
        data = response.json()
        if not data.get('ok'):
            _logger.warning('support_bridge_client: kontrol reddedildi (bağlantı %s): %s',
                             self.id, data.get('error'))
            return
        for item in data.get('events', []):
            event_id = item.get('id') or 0
            try:
                with self.env.cr.savepoint():
                    self._deliver_one(item)
            except Exception:
                # Teslim edilemeyen tek bir olay tüm kuyruğu tıkamamalı:
                # logla, imleci ilerlet, devam et.
                _logger.exception(
                    'support_bridge_client: teslim edilemeyen olay atlandı %s (bağlantı %s)',
                    event_id, self.id)
                if event_id > self.last_poll_cursor:
                    self.write({'last_poll_cursor': event_id})
        self.write({'last_synced': fields.Datetime.now()})

    @api.model
    def _cron_poll_all(self):
        for connection in self.search([('state', '=', 'connected')]):
            try:
                connection._poll_one()
            except Exception:
                _logger.exception('support_bridge_client: bağlantı %s kontrol edilirken beklenmeyen hata', connection.id)
