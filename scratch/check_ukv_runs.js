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

function fmt(dt) {
  const p = n => String(n).padStart(2, '0');
  return `${dt.getUTCFullYear()}${p(dt.getUTCMonth()+1)}${p(dt.getUTCDate())}T${p(dt.getUTCHours())}${p(dt.getUTCMinutes())}Z`;
}

async function checkFile(runTs, hours) {
  const runDt = new Date(
    Date.UTC(+runTs.slice(0,4), +runTs.slice(4,6)-1, +runTs.slice(6,8), +runTs.slice(9,11), +runTs.slice(11,13))
  );
  const validDt = new Date(runDt.getTime() + hours * 3600000);
  const validTs = fmt(validDt);
  const off = `PT${String(hours).padStart(4,'0')}H00M`;
  const prefix = `uk-deterministic-2km/${runTs}/${validTs}-${off}-rainfall_rate.nc`;
  const xml = await get(`https://met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com/?list-type=2&prefix=${prefix}`);
  return xml.includes('<Contents>');
}

async function main() {
  const runs = ['20260707T1800Z','20260707T1500Z','20260707T1200Z','20260707T0900Z','20260707T0600Z','20260707T0300Z','20260707T0000Z'];
  for (const r of runs) {
    const t1 = await checkFile(r, 1);
    const t54 = await checkFile(r, 54);
    console.log(r, 'T+1h:', t1, ' T+54h:', t54);
  }
}
main();
