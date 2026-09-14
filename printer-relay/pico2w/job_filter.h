#ifndef JOB_FILTER_H
#define JOB_FILTER_H

#include <stdint.h>
#include <stdbool.h>

// Streaming port of relay_daemon.py's _rewrite_paper_source, applied as the job
// is captured (so what lands in the store is already rewritten and its length is
// known for LPR). Two transforms:
//   - PCL:  ESC&l<N>[Hh]  ->  ESC&l7[Hh]   (paper source -> auto-select), streamed
//   - PJL:  inject "@PJL SET MEDIASOURCE=<ms>\n" before "@PJL ENTER LANGUAGE"
//           (only if mediasource is non-empty; done on the buffered job prefix)

typedef void (*job_sink_fn)(const uint8_t* data, uint32_t len, void* ctx);

// Begin a job. mediasource "" disables PJL injection (PCL rewrite still runs).
void job_filter_begin(const char* mediasource);

// Feed captured bytes; filtered output is delivered via sink(...).
void job_filter_write(const uint8_t* in, uint32_t len, job_sink_fn sink, void* ctx);

// Flush any buffered/partial state at end of job.
void job_filter_end(job_sink_fn sink, void* ctx);

// Diagnostics for the current/last job.
void job_filter_stats(uint32_t* pcl_rewrites, bool* pjl_injected);

#endif // JOB_FILTER_H
