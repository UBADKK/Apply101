import { describe, expect, it } from 'vitest';
import { outdatedParts } from './StaleMatchNotice';

describe('outdatedParts', () => {
  it('counts no current analysis as missing, whatever analysis id the result stored', () => {
    const missing = [
      { part: 'profile', missing: true },
      { part: 'job', missing: true },
    ];
    expect(outdatedParts({ profile_analysis_id: null, job_analysis_id: null }, null, null)).toEqual(missing);
    expect(outdatedParts({ profile_analysis_id: 11, job_analysis_id: 21 }, null, null)).toEqual(missing);
  });

  it('flags a newer analysis as older, not missing', () => {
    expect(outdatedParts({ profile_analysis_id: 11, job_analysis_id: 21 }, 12, 21)).toEqual([
      { part: 'profile', missing: false },
    ]);
    expect(outdatedParts({ profile_analysis_id: null, job_analysis_id: 21 }, 12, 22)).toEqual([
      { part: 'profile', missing: false },
      { part: 'job', missing: false },
    ]);
  });

  it('flags nothing for current analyses or ids that are not known yet', () => {
    expect(outdatedParts({ profile_analysis_id: 11, job_analysis_id: 21 }, 11, 21)).toEqual([]);
    expect(outdatedParts({ profile_analysis_id: null, job_analysis_id: null }, undefined, undefined)).toEqual([]);
  });
});
