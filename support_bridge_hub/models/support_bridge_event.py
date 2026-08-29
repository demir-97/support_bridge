from datetime import timedelta

from odoo import api, fields, models

# Olaylar bir teslimat kuyruğudur, arşiv değil: her client'ın kuyruğu çekmesi
# için yeterli süre geçtikten sonra (cron'ları dakikada bir çalışır) eski
# satırlar yalnızca ölü ağırlıktır.
EVENT_RETENTION_DAYS = 30


class SupportBridgeEvent(models.Model):
    _name = 'support.bridge.event'
    _description = 'Support Bridge Outbound Event Queue'
    _order = 'id'

    customer_id = fields.Many2one(
        'support.bridge.customer', required=True, ondelete='cascade', index=True)
    # Olayın ait olduğu proje; karşı taraf hangi alt kanala yazacağını buradan
    # türeyen jetondan bulur. Proje silinirse olayın teslim edilecek bir yeri
    # kalmaz, o yüzden satır da gider. 'project_sync' olayları tek bir projeye
    # ait olmadığı için burası boş kalabilir.
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
