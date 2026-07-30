const https = require('https');

function get(url) {
  return new Promise((resolve, reject) => {
    https.get(url, res => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => resolve(data));
    }).on('error', reject);
  });
}

async function listAll(prefix) {
  let keys = [];
  let token = null;
  do {
    let url = `https://met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com/?list-type=2&prefix=${encodeURIComponent(prefix)}&max-keys=1000`;
    if (token) url += `&continuation-token=${encodeURIComponent(token)}`;
    const xml = await get(url);
    const matches = [...xml.matchAll(/<Key>([^<]+)<\/Key>/g)].map(m => m[1]);
    keys.push(...matches);
    const tm = xml.match(/<NextContinuationToken>([^<]+)<\/NextContinuationToken>/);
    token = tm ? tm[1] : null;
  } while (token);
  return keys;
}

async function main() {
  const runs = ['20260707T1800Z','20260707T1500Z','20260707T1200Z','20260707T0900Z','20260707T0600Z','20260707T0300Z'];
  for (const r of runs) {
    const keys = await listAll(`uk-deterministic-2km/${r}/`);
    const rainfallKeys = keys.filter(k => k.includes('-rainfall_rate.nc'));
    const hours = rainfallKeys.map(k => {
      const m = k.match(/-PT(\d+)H\d+M-rainfall_rate\.nc$/);
      return m ? parseInt(m[1]) : null;
    }).filter(h => h !== null);
    hours.sort((a,b) => a-b);
    const maxH = hours.length ? Math.max(...hours) : null;
    console.log(`${r}: total files=${keys.length}  rainfall_rate files=${rainfallKeys.length}  max T+ hour=${maxH}`);
  }
}
main();
