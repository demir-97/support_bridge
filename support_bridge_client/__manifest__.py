{
    'name': 'Support Bridge Client',
    'version': '19.0.1.1.0',
    'category': 'Productivity',
    'author': 'CodeQuarters',
    'license': 'OPL-1',
    'summary': 'Support Bridge Client.',
    'description': """Support Bridge Client""",
    'depends': ['mail'],
    'images': ['static/description/banner.png'],
    'data': [
        'security/ir.model.access.csv',
        'data/support_bridge_cron.xml',
        'views/support_bridge_connection_views.xml',
    ],
    'installable': True,
    'application': False,
}
