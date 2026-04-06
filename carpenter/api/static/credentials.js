/* Credential form page logic */
function submitCred(e) {
    e.preventDefault();
    var val = document.getElementById('credential').value;
    fetch('__PROVIDE_URL__', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({value: val})
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.stored) {
            document.body.innerHTML = '<div style="padding:40px;text-align:center;">' +
                '<h2 style="color:#a6e22e;">Credential Stored</h2>' +
                '<p>You can close this page.</p></div>';
        } else {
            alert('Error: ' + (data.error || 'unknown'));
        }
    }).catch(function(err) { alert('Error: ' + err); });
}
