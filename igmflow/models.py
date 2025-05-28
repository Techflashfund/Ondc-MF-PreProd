from django.db import models
from ondc.models import Transaction
# Create your models here.


class OnIssue(models.Model):
        transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='full_on_issue')
        message_id = models.CharField(max_length=100)
        payload = models.JSONField()
        timestamp = models.DateTimeField()

        def __str__(self):
             return f"{self.transaction.transaction_id} - {self.message_id}"

class OnIssueStatus(models.Model):
        transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='full_on_issue_status')
        message_id = models.CharField(max_length=100)
        payload = models.JSONField()
        timestamp = models.DateTimeField()

        def __str__(self):
             return f"{self.transaction.transaction_id} - {self.message_id}"