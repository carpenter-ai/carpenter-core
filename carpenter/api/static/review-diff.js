/* Diff review page logic */
function requestAIReview() {
    var model = document.getElementById('ai-model').value;
    fetch('__AI_REVIEW_URL__', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model: model})
    }).then(function(r) {
        if (!r.ok) return r.json().then(function(d) { throw new Error(d.detail || 'Request failed'); });
        return r.json();
    }).then(function(data) {
        alert('AI review requested (arc #' + data.reviewer_arc_id + '). Refresh to see status.');
        location.reload();
    }).catch(function(err) {
        alert('Error: ' + err.message);
    });
}

function decide(decision) {
    var comment = document.getElementById('comment').value;
    if (decision === 'revise' && !comment.trim()) {
        alert('Please provide feedback for revision.');
        return;
    }
    _submitDecision({decision: decision, comment: comment}, decision);
}

function acceptAndTrigger() {
    var comment = document.getElementById('comment').value;
    var followup = document.getElementById('followup-goal').value;
    if (!followup.trim()) {
        alert('Please describe the follow-up work.');
        return;
    }
    _submitDecision(
        {decision: 'approve', comment: comment, followup_goal: followup},
        'approve'
    );
}

function _submitDecision(payload, decision) {
    fetch('__DECIDE_URL__', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    }).then(function(r) { return r.json(); }).then(function(data) {
        var colors = {approve: '#a6e22e', reject: '#f92672', revise: '#e6db74'};
        var followupHtml = '';
        if (data.followup_arc_id) {
            followupHtml = '<p style="color:#66d9ef;">Follow-up planner arc created: arc #' +
                data.followup_arc_id + '</p>';
        }
        document.body.innerHTML = '<div style="padding:40px;text-align:center;">' +
            '<h2 style="color:' + (colors[decision] || '#f8f8f2') + '">' +
            decision.toUpperCase() + '</h2>' +
            '<p>Review recorded.</p>' + followupHtml + '</div>';
    }).catch(function(err) {
        alert('Error: ' + err);
    });
}
