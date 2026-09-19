// promptfoo custom assertion for the vision/OCR extraction suite.
// Deterministic, ground-truth-based check — deliberately NOT an llm-rubric judge (see the
// note at the top of promptfoo-vision-pc.yaml / promptfoo-vision-pi.yaml for why).
//
// Checks two things against the values you typed in as vars for each test case:
//   1. Does the model's vendor field contain the expected vendor name (case-insensitive)?
//   2. Is the model's total within 1 cent of the expected total?
// Extend this if you want to also check `date` or specific line items — same pattern.

module.exports = function checkFields(output, context) {
  function extractJson(str) {
    // Strip markdown fences if the model added them despite being told not to
    // (this is the exact failure mode already documented in this project's text
    // extraction suites — qwen2.5:latest on the PC, qwen2.5:1.5b on the Pi).
    const fenced = str.match(/```(?:json)?\s*([\s\S]*?)```/);
    const candidate = fenced ? fenced[1] : str;

    // Find every complete TOP-LEVEL {...} object in the text, respecting string
    // literals (so quotes/braces inside item names etc. don't confuse the scan).
    // A reasoning model (minicpm-v4.6 on the Pi vision suite, Sept 19-20 2026)
    // "thinks out loud" before its real answer, and that reasoning text can
    // itself contain JSON-looking fragments (e.g. "items are [{"item": ...}]")
    // well before the actual final answer, and/or trailing commentary can follow
    // the real answer. The old approach (first "{" .. last "}" in the whole
    // string) spanned all of that and never parsed. Instead: collect every
    // self-contained top-level object and take the LAST one that actually
    // parses as JSON — the model's real answer is reliably the last complete
    // object it emits, even when there's chatter before or after it.
    const objects = [];
    let depth = 0;
    let start = -1;
    let inString = false;
    let escapeNext = false;

    for (let i = 0; i < candidate.length; i++) {
      const ch = candidate[i];

      if (escapeNext) {
        escapeNext = false;
        continue;
      }
      if (ch === '\\' && inString) {
        escapeNext = true;
        continue;
      }
      if (ch === '"') {
        inString = !inString;
        continue;
      }
      if (inString) continue;

      if (ch === '{') {
        if (depth === 0) start = i;
        depth++;
      } else if (ch === '}') {
        if (depth > 0) {
          depth--;
          if (depth === 0 && start !== -1) {
            objects.push(candidate.slice(start, i + 1));
            start = -1;
          }
        }
      }
    }

    if (objects.length === 0) {
      throw new Error('no JSON object found in output');
    }

    let lastError = null;
    for (let i = objects.length - 1; i >= 0; i--) {
      try {
        return JSON.parse(objects[i]);
      } catch (e) {
        lastError = e;
      }
    }
    throw new Error(
      `found ${objects.length} candidate object(s) but none parsed as valid JSON (last error: ${lastError.message})`
    );
  }

  try {
    const data = extractJson(output);
    const expectedVendor = String(context.vars.expectedVendor || '').toLowerCase();
    const expectedTotal = parseFloat(context.vars.expectedTotal);

    const gotVendor = String(data.vendor || '').toLowerCase();
    const gotTotal = parseFloat(data.total);

    const vendorOk = expectedVendor.length > 0 && gotVendor.includes(expectedVendor);
    const totalOk = !isNaN(gotTotal) && Math.abs(gotTotal - expectedTotal) < 0.01;

    if (vendorOk && totalOk) {
      return { pass: true, score: 1, reason: `vendor and total both matched (vendor="${data.vendor}", total=${data.total})` };
    }

    return {
      pass: false,
      score: (vendorOk ? 0.5 : 0) + (totalOk ? 0.5 : 0),
      reason: `vendor match=${vendorOk} (got "${data.vendor}", expected to contain "${context.vars.expectedVendor}"), total match=${totalOk} (got ${data.total}, expected ${context.vars.expectedTotal})`,
    };
  } catch (e) {
    return { pass: false, score: 0, reason: `Could not parse a JSON object from the output: ${e.message}` };
  }
};
