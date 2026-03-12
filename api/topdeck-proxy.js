export const config = { runtime: 'edge' };

const TOPDECK_API_KEY = '7454dc38-e7af-438b-b690-fadaeef45d0d';

export default async function handler(request) {
  const { searchParams } = new URL(request.url);
  const tid = searchParams.get('tid');

  if (!tid) {
    return Response.json({ error: 'Missing tid' }, { status: 400 });
  }

  let resp;
  try {
    resp = await fetch(`https://topdeck.gg/api/v2/tournaments/${tid}/rounds`, {
      headers: { 'Authorization': TOPDECK_API_KEY },
    });
  } catch (e) {
    return Response.json({ error: e.message }, { status: 502 });
  }

  const text = await resp.text();
  return new Response(text, {
    status: resp.status,
    headers: { 'Content-Type': 'application/json' },
  });
}
