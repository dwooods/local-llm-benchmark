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
    const firstBrace = candidate.indexOf('{');
    const lastBrace = candidate.lastIndexOf('}');
    if (firstBrace === -1 || lastBrace === -1 || lastBrace <= firstBrace) {
      throw new Error('no JSON object found in output');
    }
    return JSON.parse(candidate.slice(firstBrace, lastBrace + 1));
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