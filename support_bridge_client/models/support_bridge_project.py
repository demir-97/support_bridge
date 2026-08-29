from odoo import _, fields, models


class SupportBridgeProject(models.Model):
    """A project the vendor runs for us, and its chat channel.

    Records come from the vendor and are never created here: they announce
    their projects with a token each, and every one gets a sub-channel under
    the vendor's group. That way several jobs with the same vendor do not run
    together in one thread.
    """
    _name = 'support.bridge.project'
    _description = 'Support Bridge Vendor Project'
    _order = 'name'

    connection_id = fields.Many2one(
        'support.bridge.connection', required=True, ondelete='cascade', index=True)
    remote_id = fields.Integer(
        string='Vendor Project Id', required=True, readonly=True, copy=False, index=True,
        help="The project's own id on the vendor's side. Records are matched on "
             "this, never on the token: a token is a password and can be "
             "rotated, and matching on it would fork the conversation.",
    )
    token = fields.Char(
        required=True, readonly=True, copy=False,
        groups='base.group_system',
        help="Credential proving which project a message belongs to. Issued by "
             "the vendor and replaced whenever they rotate it.",
    )
    name = fields.Char(string='Project', readonly=True)
    team_name = fields.Char(
        string='Vendor Team', readonly=True,
        help="Which of the vendor's support teams handles this project.",
    )
    channel_id = fields.Many2one(
        'discuss.channel', string='Channel', readonly=True, copy=False,
        index='btree_not_null',
        help="The Discuss sub-channel for this project, nested under the "
             "vendor's group.",
    )
    active = fields.Boolean(
        default=True,
        help="Cleared when the vendor stops sharing this project. The channel "
             "and its history stay, but no new messages travel either way.",
    )

    _remote_unique = models.UniqueIndex("(connection_id, remote_id)")

    def _ensure_channel(self):
        """The project's sub-channel, hung under the vendor's group."""
        self.ensure_one()
        parent = self.connection_id.channel_id
        if self.channel_id or not parent:
            return self.channel_id
        self.channel_id = self.env['discuss.channel'].sudo().create({
            'name': self.name or _('Project'),
            'channel_type': parent.channel_type,
            'parent_channel_id': parent.id,
            'channel_member_ids': [
                (0, 0, {'partner_id': partner.id})
                for partner in self.connection_id.member_user_ids.partner_id
            ],
        })
        return self.channel_id

    def _sync_from_hub(self, connection, items):
        """Reconcile the vendor's announced list with the local records.

        Projects missing from the list are archived, not deleted: when the
        vendor stops sharing one the conversation must stop, but the history
        must not disappear.
        """
        Project = self.sudo()
        # active_test=False matters: if an archived project cannot be found
        # when the vendor shares it again, we would create a duplicate and hit
        # the unique index.
        existing = {p.remote_id: p for p in Project.with_context(active_test=False).search(
            [('connection_id', '=', connection.id)]) if p.remote_id}
        seen = set()
        for item in items or []:
            remote_id = item.get('remote_id') or 0
            token = (item.get('token') or '').strip()
            if not remote_id or not token:
                continue
            seen.add(remote_id)
            values = {
                'name': item.get('name') or '',
                'team_name': item.get('team_name') or '',
                # The token is refreshed on every sync: when the vendor
                # rotates it the record stays, only its password changes.
                'token': token,
                'active': True,
            }
            project = existing.get(remote_id)
            if project:
                resumed = not project.active
                project.write(values)
                if resumed:
                    # We announced the stop, so we announce the resume too;
                    # otherwise a dead channel quietly comes back to life.
                    project._post_notice(_(
                        "%s is sharing this project again. Messages written "
                        "here are delivered from now on.",
                        connection.partner_id.name or _('Your vendor')))
            else:
                project = Project.create(dict(
                    values, connection_id=connection.id, remote_id=remote_id))
            project._ensure_channel()
            if project.channel_id and project.channel_id.name != project.name:
                project.channel_id.sudo().name = project.name
        stale = [p for remote_id, p in existing.items() if remote_id not in seen and p.active]
        for project in stale:
            project.active = False
            # The channel stays, so without a notice people keep writing
            # here believing their messages still arrive.
            project._post_notice(_(
                "%s has stopped sharing this project. Messages written here "
                "are no longer delivered. The history stays for reference.",
                connection.partner_id.name or _('Your vendor')))

    def _post_notice(self, body):
        """An informational note inside the channel. Sent as 'notification', so
        the bridge does not carry it to the other side."""
        self.ensure_one()
        if self.channel_id:
            self.channel_id.sudo().message_post(
                body=body, message_type='notification', subtype_xmlid='mail.mt_note')

    def _warn_not_delivered(self):
        """Warn once when someone writes into a channel that stopped sharing;
        skipped when the previous message is already a warning, so warnings do
        not pile up."""
        self.ensure_one()
        if not self.channel_id:
            return
        latest = self.channel_id.message_ids[:1]
        if latest and latest.message_type == 'notification':
            return
        self._post_notice(_(
            "Not delivered — %s is no longer sharing this project with you.",
            self.connection_id.partner_id.name or _('your vendor')))

    def _find_by_token(self, connection, token):
        """Which project an incoming event belongs to. The search is always
        scoped to the connection, so a token cannot reach another vendor's
        project."""
        token = (token or '').strip()
        if not token or not connection:
            return self.browse()
        return self.sudo().search([
            ('connection_id', '=', connection.id),
            ('token', '=', token),
        ], limit=1)


