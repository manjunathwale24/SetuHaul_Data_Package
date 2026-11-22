import { ComponentFixture, TestBed } from '@angular/core/testing';
import { DevopsDashboardComponent } from './devops-dashboard.component';

describe('DevopsDashboardComponent', () => {
  let component: DevopsDashboardComponent;
  let fixture: ComponentFixture<DevopsDashboardComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: []
    })
    .compileComponents();

    fixture = TestBed.createComponent(DevopsDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create the component', () => {
    expect(component).toBeTruthy();
  });
});