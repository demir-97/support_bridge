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
    """Send in a daemon thread so the agent's own transaction never waits on
    a slow or dead customer server. Only for best-effort sends that have a
    guaranteed fallback (the customer's poll cron); errors are logged and
    swallowed. Schedule it with cr.postcommit so it only fires for data that
    actually committed."""
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
        # index: every authenticated request looks the customer up by this.
        required=True, copy=False, readonly=True, index=True, groups='base.group_system',
        help="Hand this key, together with this server's address, to the customer — "
             "they paste both into their Support Bridge Client settings to connect.",
    )
    partner_id = fields.Many2one(
        'res.partner', string='Customer Contact', readonly=True, copy=False,
        help="Auto-created contact representing the customer's company; relayed "
             "messages are attributed to individual contacts nested under it.",
    )
    project_ids = fields.One2many(
        'project.project', 'support_bridge_customer_id', string='Bridged Projects',
        help="The projects this customer talks to you about. Each one has its "
             "own chat channel on both sides and its own revocable token.",
    )
    project_count = fields.Integer(compute='_compute_project_count')
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
            record._ensure_partner()
        return records

    def _ensure_partner(self):
        """The customer's persona contact. Creates no channel: chat channels
        belong to projects and open when a project is shared."""
        self.ensure_one()
        if not self.partner_id:
            self.partner_id = self.env['res.partner'].sudo().create({
                'name': self.name,
                'is_company': True,
                'comment': _('Support Bridge customer persona — do not merge or delete.'),
            })

    def _update_remote_name(self, name):
        """Called on every /ping with the customer's own company name, so the
        contact and the channels carry their real name rather than the
        placeholder typed when the API key was created."""
        self.ensure_one()
        name = (name or '').strip()
        if not name or name == self.name:
            return
        self.name = name
        if self.partner_id:
            self.partner_id.sudo().name = name
        # Sub-channel names carry the customer name as a prefix; refresh all.
        for project in self.project_ids:
            project._sync_support_bridge()

    def _update_public_url(self, public_url):
        """Called on every /ping: store or clear the customer's push address so
        it matches whatever they currently have configured."""
        self.ensure_one()
        public_url = (public_url or '').strip() or False
        if public_url != self.client_public_url:
            self.client_public_url = public_url

    def _serialize_projects(self):
        """The minimum the customer needs to build its sub-channels. The team
        name travels too: their grouping is by vendor, not by team, but seeing
        it tells them which team they are talking to."""
        self.ensure_one()
        return [{
            # Identity is the project id here, not the token. A token is a
            # password and can be rotated; keying on it would give the far side
            # a second record and a second channel on every rotation, splitting
            # the history in two.
            'remote_id': project.id,
            'token': project.support_bridge_token,
            'name': project.name or '',
            'team_name': project.support_bridge_team_id.name or '',
        } for project in self.project_ids.sudo().filtered('support_bridge_token')]

    def _enqueue_project_sync(self):
        """Tell the customer the project list changed.

        The whole list travels, not a delta. The receiving side already works
        from a full list -- create, update, archive what is missing -- so a
        full list is order-independent and a missed event is repaired by the
        next one.

        It rides the same queue as messages: pushed at once to a reachable
        customer, picked up within a minute by everyone else. Nobody has to
        press Connect again."""
        self.ensure_one()
        return self._enqueue_event('project_sync', {'projects': self._serialize_projects()}, None)

    def _enqueue_event(self, event_type, payload, project):
        """Record one outbound event for this customer. The queue is the single
        source both delivery paths read: the customer's poll cron walks it by
        event id, and a customer that declared itself reachable also gets the
        same event pushed immediately.

        Message and reaction events always belong to a project; the far side
        finds the right sub-channel from the token in the payload."""
        self.ensure_one()
        event = self.env['support.bridge.event'].sudo().create({
            'customer_id': self.id,
            'project_id': project.id if project else False,
            'event_type': event_type,
            'payload': payload,
        })
        self._push_to_client(event)
        return event

    def _serialize_event(self, event):
        """The wire format of an event; push and poll both use it. Attachment
        contents are not stored on the event row but read fresh at this point,
        which keeps the queue table small."""
        self.ensure_one()
        data = dict(event.payload or {})
        data['id'] = event.id
        data['type'] = event.event_type
        # The token is read from the project, not copied onto the event, so
        # revoking a project also strands events already sitting in its queue.
        data['project_token'] = event.project_id.sudo().support_bridge_token or ''
        data['project_name'] = event.project_id.name or ''
        if event.event_type == 'project_sync':
            # The list must be the truth right now rather than a copy taken
            # when the event was queued, so even a stale sync event carries it.
            data['projects'] = self._serialize_projects()
        if event.event_type == 'message' and data.get('message_id'):
            message = self.env['mail.message'].sudo().browse(data['message_id']).exists()
            if message:
                data['attachments'] = self._serialize_attachments(message.attachment_ids)
                # The recipient must see which attachment did not make it,
                # otherwise they act on incomplete information.
                data['skipped_attachments'] = self._partition_attachments(
                    message.attachment_ids)[1]
            else:
                data['attachments'] = []
        return data

    @api.model
    def _partition_attachments(self, attachments):
        """Return (attachments to send, labels of the ones that cannot go).

        The single place the size and count limits live. It reads metadata
        only, never attachment contents, so calling it just to answer "what
        gets skipped?" is cheap.
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

    def _warn_skipped_attachments(self, channel, skipped):
        """Tell the sender, inside the channel, which attachments did not go.
        Dropping them quietly leaves someone believing the file arrived. Sent
        as 'notification', and the bridge only relays 'comment', so the warning
        stays on this side."""
        self.ensure_one()
        if not skipped or not channel:
            return
        channel.sudo().message_post(
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
        """Turn the wire format into the attachment tuples message_post wants."""
        result = []
        for item in (items or [])[:MAX_ATTACHMENTS_PER_MESSAGE]:
            name = item.get('name') or 'file'
            try:
                raw = base64.b64decode(item.get('datas') or '')
            except Exception:
                # The sender already enforces the limits, so anything that
                # fails here is corrupt or tampered with -- do not swallow it.
                _logger.warning('support_bridge_hub: could not decode attachment, skipped: %s', name)
                continue
            if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
                _logger.warning('support_bridge_hub: attachment rejected (empty or over the limit): %s', name)
                continue
            result.append((name, raw))
        return result

    def _apply_client_reaction(self, project, message_id, content, action, author_name,
                               author_remote_id=None, author_email=None):
        """Apply a reaction relayed from the customer to the right message in
        that project's channel, as the contact of whoever reacted.
        _message_reaction both stores it and fires the live bus notification,
        so agents see it appear and disappear at once.

        We check the message really sits in that project's channel: otherwise
        a valid token could react to a message in some other channel."""
        self.ensure_one()
        message = self.env['mail.message'].sudo().browse(int(message_id or 0)).exists()
        channel = project.sudo().support_bridge_channel_id
        if not message or message.model != 'discuss.channel' or message.res_id != channel.id:
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
        """Best-effort instant delivery for customers that declared themselves
        reachable. Fired after commit on its own thread so the agent never
        waits on the customer's network. No queueing and no retry: if it fails
        for any reason the customer's own poll cron is the guaranteed
        fallback, which is why failing quietly is the right behaviour."""
        self.ensure_one()
        if not self.client_public_url:
            return
        url = self.client_public_url + '/support_bridge/deliver'
        headers = {'Authorization': 'Bearer %s' % self.api_key}
        # Serialize now, while still in the transaction (attachment contents
        # included); the callback itself must not touch the ORM after commit.
        payload = self._serialize_event(event)
        self.env.cr.postcommit.add(
            lambda: _fire_and_forget_post(url, headers, payload, PUSH_TIMEOUT))

    def _get_or_create_remote_author(self, name, remote_id=None, email=None):
        """The real author on the customer's side has no user account here, so
        relayed messages are attributed to a small per-person contact nested
        under the customer persona rather than to the persona itself. That way
        different people show up in Discuss under their own names.

        Matching is on the partner id from the far side. The name is a display
        label and is refreshed whenever it changes over there. Matching on the
        name would fail twice over: two people sharing a name would collapse
        into one contact, and renaming someone would open a second contact and
        split their history. Email travels too, but only as a distinguishing
        attribute -- never as the matching key, since it changes and shared
        mailboxes belong to several people.
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
                # Older contacts carry no id (they were created by name
                # before this existed); adopt them on their first message.
                contact = Partner.search(
                    domain + [('support_bridge_hub_remote_id', '=', False),
                              ('name', '=', name)], limit=1)
                if contact:
                    contact.support_bridge_hub_remote_id = remote_id
        elif name:
            # The far side sends no id yet (older version) -- fall back to name.
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
        """True when partner is this customer's persona, or one of the per-person
        contacts _get_or_create_remote_author opened under it."""
        self.ensure_one()
        return bool(partner) and (partner.id == self.partner_id.id or partner.parent_id.id == self.partner_id.id)

    @api.depends('project_ids')
    def _compute_project_count(self):
        for record in self:
            record.project_count = len(record.project_ids)

    def action_open_projects(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Projects'),
            'res_model': 'project.project',
            'domain': [('support_bridge_customer_id', '=', self.id)],
            'view_mode': 'list,form',
            'target': 'current',
        }
