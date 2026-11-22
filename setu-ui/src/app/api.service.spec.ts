import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { ApiService } from './api.service';

describe('ApiService Integration', () => {
  let service: ApiService;
  let httpTestingController: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [ApiService]
    });
  });

  beforeEach(() => {
    service = TestBed.inject(ApiService);
    httpTestingController = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpTestingController.verify();
  });

  it('should call Lambda get_activity endpoint', () => {
    const mockResponse = {
      status: 'ok',
      activity_log: [
        { source: 'deployment', severity: 'high', message: 'Test failure', target: 'cluster' }
      ]
    };

    service.getActivity().subscribe({
      next: (result) => {
        expect(result.status).toBe('ok');
        expect(result.activity_log.length).toBe(1);
      }
    });

    const req = httpTestingController.expectOne(service['lambdaUrl']);
    expect(req.request.method).toBe('POST');
    req.flush(mockResponse);
  });

  it('should call Lambda get_status endpoint', () => {
    const mockResponse = { status: 'ok', cluster: 'setuhaul-cluster' };

    service.getStatus().subscribe({
      next: (result) => {
        expect(result.status).toBe('ok');
        expect(result.cluster).toBe('setuhaul-cluster');
      }
    });

    const req = httpTestingController.expectOne(service['lambdaUrl']);
    expect(req.request.method).toBe('POST');
    req.flush(mockResponse);
  });

  it('should call K8s health endpoint', () => {
    const mockResponse = { status: 'healthy' };

    service.checkHealth().subscribe({
      next: (result) => {
        expect(result.status).toBe('healthy');
      }
    });

    const req = httpTestingController.expectOne(service['kubUrl']);
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });
});