// Inspired by: https://anthonymineo.com/docker-healthcheck-for-your-node-js-app/
const http = require('http');
const baseUrl = new URL(process.env.APP_BASE_URL);
const options = {
    host: '127.0.0.1',
    port: 22300,
    timeout: 2000,
    path: '/api/ping',
    headers: {
        'Host': baseUrl.host,
        'Origin': baseUrl.origin,
    }
};

const healthCheck = http.request(options, (res) => {
    console.log(`HEALTHCHECK STATUS: ${res.statusCode}`);
    if (res.statusCode === 200) {
        process.exit(0);
    }
    else {
        process.exit(1);
    }
});

healthCheck.on('error', function (err) {
    console.error('ERROR:' + err);
    process.exit(1);
});

healthCheck.end();
