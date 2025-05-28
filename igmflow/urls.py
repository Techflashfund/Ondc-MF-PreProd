from django.urls import path
from .views import *

urlpatterns=[
    path('issue/',IGMIssue.as_view(),name='issue'),
    path('on_issue',OnIssueView.as_view(),name='on_issue'),

    path('issue_status',IssueStatusView.as_view(),name='issue_status'),
    path('on_issue_status',OnIssueStatusView.as_view(),name='on_issue_status'),

    path('close',IssueCloseView.as_view(),name='close')
]