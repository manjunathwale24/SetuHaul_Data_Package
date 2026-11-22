import { Component, OnInit } from '@angular/core';

@Component({
  selector: 'app-dashboard',
  templateUrl: './dashboard.component.html',
  styleUrls: ['./dashboard.component.scss']
})
export class DashboardComponent implements OnInit {
  activityLog: any[] = [];
  loading = false;
  error: string | null = null;

  ngOnInit(): void {
  }
}