from datetime import timedelta

from odoo import api, fields, models

# This is a delivery queue, not an archive. Clients poll every minute, so rows
# older than this have been fetched long ago and are dead weight.
EVENT_RETENTION_DAYS = 30


class SupportBridgeEvent(models.Model):
    _name = 'support.bridge.event'
    _description = 'Support Bridge Outbound Event Queue'
    _order = 'id'

    customer_id = fields.Many2one(
        'support.bridge.customer', required=True, ondelete='cascade', index=True)
    # The project this event belongs to. The far side finds the right sub-channel
    # from the token this points at. Deleting the project leaves the event with
    # nowhere to go, so the row goes too. Empty for 'project_sync', which is
    # about the whole list rather than one project.
    project_id = fields.Many2one(
        'project.project', ondelete='cascade', index='btree_not_null')
    event_type = fields.Selection([
        ('message', 'Message'),
        ('reaction_add', 'Reaction Added'),
        ('reaction_remove', 'Reaction Removed'),
        ('project_sync', 'Project List Changed'),
    ], required=True)
    payload = fields.Json()

    @api.autovacuum
    def _gc_events(self):
        cutoff = fields.Datetime.now() - timedelta(days=EVENT_RETENTION_DAYS)
        self.search([('create_date', '<', cutoff)]).unlink()
