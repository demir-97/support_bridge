{
    'name': 'Support Bridge Hub',
    'version': '19.0.1.1.0',
    'category': 'Productivity',
    'author': 'CodeQuarters',
    'license': 'OPL-1',
    'summary': 'Support Bridge Hub.',
    'description': """Support Bridge Hub""",
    'depends': ['mail', 'project', 'helpdesk'],
    'images': ['static/description/banner.png'],
    'data': [
        'security/ir.model.access.csv',
        'views/support_bridge_customer_views.xml',
        'views/project_project_views.xml',
    ],
    'installable': True,
    'application': False,
}
