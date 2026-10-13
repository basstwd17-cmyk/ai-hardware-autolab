"""Dispatch only the current three-role scenario workflow."""
class WorkflowRouter:
    def __init__(self, ledger, *, conversation):
        self.ledger, self.conversation = ledger, conversation
        self.policy = None
    def supports(self, job_id): return self.conversation.owns(job_id)
    def advance(self, job_id):
        if not self.supports(job_id): raise ValueError("unsupported historical workflow; submit a new scenario")
        return self.conversation.advance(job_id)
    def phase(self, job_id):
        return self.conversation.phase(job_id).action if self.supports(job_id) else ("finished" if self.ledger.job(job_id).state=="done" else "unavailable")
    def recover(self): return []
