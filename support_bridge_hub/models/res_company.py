from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    support_bridge_parent_channel_id = fields.Many2one(
        'discuss.channel', string='Support Parent Channel',
        help="Every customer's support channel is created as a sub-channel of this one, "
             "so they all appear neatly grouped in Discuss. Leave empty to auto-create "
             "one the first time a customer connects.",
    )
