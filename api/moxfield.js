export const config = { runtime: 'edge' };

export default async function handler(request) {
  const { searchParams } = new URL(request.url);
  const deckUrl = searchParams.get('url') || '';
  const m = deckUrl.match(/moxfield\.com\/decks\/([A-Za-z0-9_-]+)/);

  if (!m) {
    return Response.json({ detail: 'Invalid Moxfield URL.' }, { status: 400 });
  }

  let resp;
  try {
    resp = await fetch(`https://api2.moxfield.com/v2/decks/all/${m[1]}`, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.moxfield.com/',
      },
    });
  } catch (e) {
    return Response.json({ detail: `Network error: ${e.message}` }, { status: 502 });
  }

  if (!resp.ok) {
    return Response.json({ detail: `Moxfield returned ${resp.status}.` }, { status: 502 });
  }

  const data = await resp.json();

  const commanders = Object.values(data.commanders || {})
    .flatMap(e => Array(e.quantity || 1).fill(e.card.name));
  const mainboard = Object.values(data.mainboard || {})
    .flatMap(e => Array(e.quantity || 1).fill(e.card.name));

  if (!mainboard.length && !commanders.length) {
    return Response.json({ detail: 'Deck appears empty or private.' }, { status: 400 });
  }

  return Response.json({
    commander: commanders.join(' / ') || 'Unknown Commander',
    cards: mainboard,
    card_count: mainboard.length,
    name: data.name || '',
  }, {
    headers: { 'Access-Control-Allow-Origin': '*' },
  });
}
