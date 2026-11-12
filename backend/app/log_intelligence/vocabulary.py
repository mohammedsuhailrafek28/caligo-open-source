"""Closed vocabularies shared by both arms; frozen before held-out evaluation."""
CUES = {
 'failure': {'fail','failure','error','reject','decline','abort','break','lose','unavailable'},
 'retry': {'retry','replay','resend'},
 'timeout': {'timeout','unresponsive','hang'},
 'crash': {'crash','terminate','die','freeze'},
 'duplicate': {'duplicate','twice','repeat'},
 'capture': {'capture'}, 'refund': {'refund','reimburse'},
 'paid': {'pay','paid','settle'}, 'stale': {'stale','late'},
 'healthy': {'healthy','operational'},
}
DOMAINS = {'payment','merchant','database','order','inventory','stock','shipment','webhook','integration','transaction'}
FAILURE_CLUES = {'failure','retry','timeout','crash','duplicate','stale'}
