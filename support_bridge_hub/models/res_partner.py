from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    support_bridge_hub_remote_id = fields.Integer(
        string='Support Bridge Remote Contact',
        copy=False,
        # Empty on almost every contact, so only non-null rows are indexed.
        index='btree_not_null',
        help="Which person on the customer's side this auto-created contact "
             "mirrors. Contacts are matched on this id — never on the name, "
             "because two people can share a name and a person can be renamed.",
    )
