from django.db import models

class Transaction(models.Model):
    transaction_id = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50, blank=True, null=True)
    
    def __str__(self):
        return self.transaction_id

class Message(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='messages')
    message_id = models.CharField(max_length=100)
    action = models.CharField(max_length=50)
    payload = models.JSONField()
    timestamp = models.DateTimeField()

    def __str__(self):
        return f"{self.transaction.transaction_id} - {self.message_id}"

class FullOnSearch(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='full_on_searchs')
    message_id = models.CharField(max_length=100)
    payload = models.JSONField()
    timestamp = models.DateTimeField()

    def __str__(self):
        return f"{self.transaction.transaction_id} - {self.message_id}"
    

class SelectSIP(models.Model):
        transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='full_on_selects')
        message_id = models.CharField(max_length=100)
        payload = models.JSONField()
        timestamp = models.DateTimeField()

        def __str__(self):
             return f"{self.transaction.transaction_id} - {self.message_id}"
        
class SubmissionID(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE)
    message_id=models.CharField(max_length=100)
    submission_id=models.CharField(max_length=100)
    timestamp = models.DateTimeField()

    def __str__(self):
        return f"{self.transaction.transaction_id} - {self.submission_id}"
    

class OnInitSIP(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='full_on_init')
    message_id = models.CharField(max_length=100)
    payload = models.JSONField()
    timestamp = models.DateTimeField()

    def __str__(self):
            return f"{self.transaction.transaction_id} - {self.message_id}"
    

class OnConfirm(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='full_on_confirm')
    message_id = models.CharField(max_length=100)
    payload = models.JSONField()
    timestamp = models.DateTimeField()

    def __str__(self):
            return f"{self.transaction.transaction_id} - {self.message_id}"
     
class OnStatus(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='full_on_status')
    message_id = models.CharField(max_length=100)
    payload = models.JSONField()
    timestamp = models.DateTimeField()

    def __str__(self):
            return f"{self.transaction.transaction_id} - {self.message_id}"

class OnUpdate(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='full_on_update')
    message_id = models.CharField(max_length=100)
    payload = models.JSONField()
    timestamp = models.DateTimeField()

    def __str__(self):
            return f"{self.transaction.transaction_id} - {self.message_id}"
    
class OnCancel(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='full_on_cancel')
    message_id = models.CharField(max_length=100)
    payload = models.JSONField()
    timestamp = models.DateTimeField()

    def __str__(self):
            return f"{self.transaction.transaction_id} - {self.message_id}"
     
     


class PaymentSubmisssion(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE)
    message_id=models.CharField(max_length=100)
    payment_id=models.CharField(max_length=100)
    status_pay=models.CharField(max_length=100)
    timestamp = models.DateTimeField()

    def __str__(self):
        return f"{self.transaction.transaction_id} - {self.submission_id}"

