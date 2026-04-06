/* Code review page logic */
function decide(decision) {
    var comment = document.getElementById('comment').value;
    fetch('__DECIDE_URL__', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({decision: decision, comment: comment})
    }).then(function(r) { return r.json(); }).then(function(data) {
        document.body.innerHTML = '<div style="padding:40px;text-align:center;">' +
            '<h2 style="color:' + (decision === 'approved' ? '#a6e22e' : '#f92672') + '">' +
            decision.toUpperCase() + '</h2>' +
            '<p>Review recorded.</p></div>';
    }).catch(function(err) {
        alert('Error: ' + err);
    });
}
