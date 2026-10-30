"""Load the reviewed envelope without giving evaluation fields to tools."""
from copy import deepcopy
from payment.cases import PaymentCase

def from_row(row):
    evaluation=row['evaluation']
    return PaymentCase(case_id=row['case_id'],dataset_kind=row['dataset_kind'],
        conversation=deepcopy(row['input']['conversation']),
        environment=deepcopy(row['runtime']['environment']),
        gold=deepcopy(evaluation['gold']),
        task_reference=deepcopy(evaluation['task_reference']),
        workflow_reference=deepcopy(evaluation.get('workflow_reference')),
        split=row['split'],review=deepcopy(row.get('review',{})),
        runtime_policy=deepcopy(row['runtime']['execution_policy']))

def uses_public_commerce(row):
    return 'public_spec' in row['runtime']['environment']
