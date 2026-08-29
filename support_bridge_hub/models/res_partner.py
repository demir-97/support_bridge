from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    support_bridge_hub_remote_id = fields.Integer(
        string='Support Bridge Remote Contact',
        copy=False,
        # Kayıtların ezici çoğunluğunda boş kalacağı için yalnızca dolu
        # satırlar indekslenir.
        index='btree_not_null',
        help="Which person on the customer's side this auto-created contact "
             "mirrors. Contacts are matched on this id — never on the name, "
             "because two people can share a name and a person can be renamed.",
    )
