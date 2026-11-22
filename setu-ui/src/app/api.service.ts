import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';

@Injectable({
  providedIn: 'root'
})
export class ApiService {
  private lambdaUrl = 'https://p5ymtoxnwldk5lt4v6wdwn6i6a0fdfof.lambda-url.ap-southeast-2.on.aws/';
  private kubUrl = 'http://ae13542555c274d28bb23bf42f7390b0-1259872480.ap-southeast-2.elb.amazonaws.com';

  constructor(private http: HttpClient) { }

  getActivity() {
    return this.http.post<any>(this.lambdaUrl, { source: 'get_activity' });
  }

  getStatus() {
    return this.http.post<any>(this.lambdaUrl, { source: 'get_status' });
  }

  checkHealth() {
    return this.http.get<any>(`${this.kubUrl}/`);
  }
}