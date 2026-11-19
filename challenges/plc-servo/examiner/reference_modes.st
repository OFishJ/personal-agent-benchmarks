(* Examiner fixture for the virtual station's four operating selections. *)
D102 := D102 + 1;
D103 := D11;
D104 := D12;
D101 := 0;
D105 := 0;
D106 := 0;
IF D1 <> 0 OR D2 <> 8 OR D3 <> 0 THEN
    M100 := FALSE;
    D202 := 0;
ELSIF D12 = 0 THEN
    M100 := D19 <> 0 AND D0 <> 0;
    D101 := D0;
ELSE
    IF D16 <> D200 AND D19 <> 0 THEN
        M100 := TRUE;
        D202 := D15;
    END_IF;
    IF D12 = 1 THEN
        D105 := D13;
    ELSIF D12 = 2 THEN
        D101 := D0;
    ELSIF D12 = 3 THEN
        IF D202 > 0 THEN
            D106 := D14;
            D202 := D202 - 1;
        ELSE
            M100 := FALSE;
        END_IF;
    END_IF;
END_IF;
D200 := D16;
