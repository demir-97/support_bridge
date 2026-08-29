import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# On transient failures the interval widens step by step; the last value is
# the ceiling and holds until the window closes. A flat 5 minutes would throw
# ~8600 requests in 30 days at a server that is down -- wasted load, and a
# beating for a server trying to come back up.
RETRY_BACKOFF_MINUTES = (5, 15, 45, 120, 360)
# How long we keep trying. Even after a week of downtime the message lands
# within six hours of the server coming back.
RETRY_WINDOW_DAYS = 30
# Sent rows are bookkeeping only ("did it reach the vendor?") -- the chat
# messages themselves live in mail.message and are never deleted. Failed rows
# are kept indefinitely as evidence of what could not get out.
SENT_RETENTION_DAYS = 30
# New rows are sent by the post-commit background thread. The cron only
# picks up pending rows old enough that the thread plainly never ran (the
# server restarted in between), so the two paths never send the same message.
PENDING_GRACE_MINUTES = 5


class SupportBridgeOutbox(models.Model):
    _name = 'support.bridge.outbox'
    _description = 'Support Bridge Outbound Message Queue'
    _order = 'id'

    connection_id = fields.Many2one('support.bridge.connection', required=True, ondelete='cascade')
    # Hangi projenin konusmasina ait; jeton gonderim aninda projeden okunur ki
    # bayi jetonu yenilerse kuyrukta bekleyen satirlar da yeni jetonla gitsin.
    project_id = fields.Many2one('support.bridge.project', required=True, ondelete='cascade', index=True)
    message_id = fields.Many2one('mail.message', ondelete='set null')
    # The far side matches its contact on this id; name and email are display
    # only. Author details are stored here as well because message_id can be
    # cleared when the message goes away.
    author_partner_id = fields.Many2one('res.partner', ondelete='set null')
    author_name = fields.Char()
    author_email = fields.Char()
    body = fields.Text()
    state = fields.Selection(
        [('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed')],
        default='pending', required=True,
    )
    attempts = fields.Integer(default=0)
    # Empty means "never again": either the vendor rejected the content for
    # good (4xx), or the retry window closed. An empty datetime never matches
    # the cron's domain, which is how that state is expressed.
    next_retry = fields.Datetime(string='Next Retry', copy=False, index='btree_not_null')
    last_error = fields.Text()

    def _try_send(self):
        self.ensure_one()
        connection = self.connection_id
        # Attachments are not copied onto the queue row but read from the
        # source message at send time, so a retry after an outage still has
        # them.
        source_attachments = self.message_id.attachment_ids if self.message_id else \
            self.env['ir.attachment']
        attachments = connection._serialize_attachments(source_attachments)
        skipped = connection._partition_attachments(source_attachments)[1]
        ok, error, hub_message_id, status_code = connection.send_message(
            self.project_id.sudo().token, self.body, self.author_name, attachments, skipped,
            author_id=self.author_partner_id.id, author_email=self.author_email)
        if ok:
            self.write({'state': 'sent', 'last_error': False, 'next_retry': False})
            if hub_message_id and self.message_id:
                map_model = self.env['support.bridge.message.map'].sudo()
                if not map_model.search_count([
                        ('connection_id', '=', connection.id),
                        ('local_message_id', '=', self.message_id.id)]):
                    map_model.create({
                        'connection_id': connection.id,
                        'local_message_id': self.message_id.id,
                        'remote_message_id': hub_message_id,
                    })
        else:
            # A 4xx is the vendor saying "this content will never be
            # accepted" (wrong key, empty message, ...), so retrying is
            # pointless and the row is dropped at once. Everything else
            # (network outage, 5xx, timeout) counts as transient and keeps
            # retrying on a widening interval until the window closes.
            attempts = self.attempts + 1
            self.write({
                'state': 'failed',
                'attempts': attempts,
                'last_error': error,
                'next_retry': self._next_retry_at(attempts, 400 <= status_code < 500),
            })

    def _next_retry_at(self, attempts, permanent):
        """When to try next, or False if there will be no next time."""
        self.ensure_one()
        if permanent:
            return False
        now = fields.Datetime.now()
        if now - (self.create_date or now) > timedelta(days=RETRY_WINDOW_DAYS):
            return False
        step = RETRY_BACKOFF_MINUTES[min(attempts, len(RETRY_BACKOFF_MINUTES)) - 1]
        return now + timedelta(minutes=step)

    @api.model
    def _cron_retry_failed(self):
        now = fields.Datetime.now()
        pending_cutoff = now - timedelta(minutes=PENDING_GRACE_MINUTES)
        # Rows with an empty next_retry never match this comparison, which
        # is how "never again" is expressed.
        outbox_rows = self.search([
            '|',
            '&', ('state', '=', 'failed'), ('next_retry', '<=', now),
            '&', ('state', '=', 'pending'), ('create_date', '<', pending_cutoff),
        ])
        for row in outbox_rows:
            try:
                row._try_send()
            except Exception:
                _logger.exception('support_bridge_client: unexpected error retrying outbox row %s', row.id)

    @api.autovacuum
    def _gc_sent_rows(self):
        cutoff = fields.Datetime.now() - timedelta(days=SENT_RETENTION_DAYS)
        self.search([('state', '=', 'sent'), ('create_date', '<', cutoff)]).unlink()
