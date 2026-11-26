// Settings object -> Camera Raw XMP document.
//
// The mapping is name-identical: the controller parameter ids Lightroom
// reports (Exposure2012, Highlights2012, HueAdjustmentRed, …) are exactly the
// local names of the crs: properties in the develop-settings XMP. Measured
// round-trip over all 151 global parameters: 141 identical, 10 "drift" cases
// that are the null-vs-default distinction (a 0 written explicitly reads back
// as null == default), which is why develop.mjs normalises through getDefault.

const CRS_NS = 'http://ns.adobe.com/camera-raw-settings/1.0/';

export function escapeXml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Format a number the way Camera Raw expects (no exponent, no trailing zeros). */
export function formatNumber(value) {
  if (!Number.isFinite(value)) return '0';
  if (Number.isInteger(value)) return String(value);
  return String(Number(value.toFixed(6)));
}

export function settingsToXmp(settings) {
  const attrs = Object.entries(settings)
    .filter(([, v]) => typeof v === 'number' || typeof v === 'string')
    .map(([name, value]) => {
      const text = typeof value === 'number' ? formatNumber(value) : escapeXml(value);
      return `crs:${name}="${text}"`;
    })
    .join('\n      ');

  return `<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
      xmlns:crs="${CRS_NS}"
      ${attrs}/>
  </rdf:RDF>
</x:xmpmeta>`;
}
