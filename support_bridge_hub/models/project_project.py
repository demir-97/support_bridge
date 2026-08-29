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
        # Only already-shared projects refresh their name. Filling in the
        # customer or the team shares nothing: opening a project record is not
        # the same as being ready to show it to the customer.
        if {'name', 'support_bridge_customer_id'} & set(vals):
            for project in self.filtered('support_bridge_shared'):
                project._sync_support_bridge()
                project.support_bridge_customer_id._enqueue_project_sync()
        return res

    def _bridge_channel_name(self):
        """Sub-channel name: 'Customer — Project'. The customer comes first
        because grouping is by team, so projects from different customers sit
        side by side and only the name says whose is whose."""
        self.ensure_one()
        customer = self.support_bridge_customer_id
        if not customer:
            return self.name or _('Project')
        return '%s — %s' % (customer.name, self.name or _('Project'))

    def _sync_support_bridge(self):
        """Keep the token, the channel and the channel name up to date.

        Telling the customer is NOT this function's job. That belongs to the
        act that starts or stops the share. Wired here, it would stay silent
        whenever the channel already exists under the same name -- which is
        exactly the case when a stopped project is shared again."""
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
            # Odoo will not reparent an existing sub-channel. Rebuilding it
            # is free while nothing has been said; once there is history we
            # refuse the change rather than split the conversation in two.
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
        """Members come from the team: its membership is the only thing that
        decides who sees this conversation."""
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
        """Share the project: open the channels, issue the token and send the
        list across. Sharing is deliberate -- opening a project record is not
        the same as being ready to show it to the customer."""
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
            # Sent on every share, the first and the fifth alike. The channel
            # may already exist, which is no reason to stay silent.
            project.support_bridge_customer_id._enqueue_project_sync()
        return True

    def action_support_bridge_revoke(self):
        """Stop sharing. Both channels and their history stay, but the customer
        is told plainly -- cutting the line quietly leaves them believing
        their messages still arrive."""
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
        """Post an informational note inside the channel. Sent as 'notification',
        and the bridge only relays 'comment', so it stays on this side."""
        self.ensure_one()
        channel = self.sudo().support_bridge_channel_id
        if channel:
            channel.message_post(body=body, message_type='notification',
                                 subtype_xmlid='mail.mt_note')

    @api.model
    def _find_bridged_by_channel(self, channel_ids):
        """{channel id: project} for projects that still hold a token. A message
        written into a revoked project's channel never leaves."""
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
        """{channel id: project} for projects whose channel remains but which are
        no longer shared."""
        if not channel_ids:
            return {}
        projects = self.sudo().search([
            ('support_bridge_channel_id', 'in', list(channel_ids)),
            ('support_bridge_shared', '=', False),
        ])
        return {p.support_bridge_channel_id.id: p for p in projects}

    def _warn_not_delivered(self):
        """Warn once when someone writes into a channel that stopped sharing.
        Repeating it on every message would bury the channel, so it is skipped
        when the previous message is already a warning."""
        self.ensure_one()
        channel = self.sudo().support_bridge_channel_id
        if not channel:
            return
        latest = channel.message_ids[:1]
        if latest and latest.message_type == 'notification':
            return
        self._post_bridge_notice(_(
            "Not delivered — this project is no longer shared with %s. "
            "Press Share on the project to resume the conversation.",
            self.support_bridge_customer_id.name or _('the customer')))

    @api.model
    def _find_by_bridge_token(self, customer, token):
        """Find which project an incoming event belongs to. The search is always
        scoped to the authenticated customer, so even a guessed token cannot
        reach another customer's project."""
        token = (token or '').strip()
        if not token or not customer:
            return self.browse()
        return self.sudo().search([
            ('support_bridge_customer_id', '=', customer.id),
            ('support_bridge_token', '=', token),
        ], limit=1)
